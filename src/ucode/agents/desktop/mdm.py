"""Publish Claude Desktop's machine policy and organization plugin."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from ucode import config_io, databricks, managed_files
from ucode.agents.desktop import claude_config, cowork, setup
from ucode.managed_config import get_managed_config, normalize_managed_config
from ucode.managed_resolve import (
    managed_enabled_tools,
    managed_provider_service,
    managed_unity_catalog_location,
    resolve_state,
)
from ucode.mcp import _resolve_managed_mcp_servers
from ucode.os_compatibility import subprocess_cross_os
from ucode.skills_download import _fetch_bundles, _resolve_managed_skills, _write_bundle
from ucode.ui import print_note
from ucode.workspace import split_workspace_key, workspace_id_args

MDM_ROOT = managed_files.CLAUDE_DESKTOP_MDM_ROOT
POLICY_PATH = managed_files.CLAUDE_DESKTOP_POLICY_PATH


def validate_mdm_execution() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Desktop MDM currently supports macOS only.")


def _check_machine_path(path: Path, *, required: bool = False) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise RuntimeError(f"Refusing linked machine configuration path: {part}")
        if not part.exists():
            if part == path and required:
                raise RuntimeError(f"Machine configuration is missing: {path}")
            continue
        info = part.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(
                f"Machine configuration must be root-owned and not writable by other users: {part}"
            )
        if stat.S_ISDIR(info.st_mode) and not info.st_mode & stat.S_IXOTH:
            raise RuntimeError(f"Machine directory must be accessible to device users: {part}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError(f"Not a regular machine configuration path: {part}")


def _check_machine_links(path: Path) -> None:
    for part in (path, *path.parents):
        if not part.is_symlink():
            continue
        if part.lstat().st_uid != 0:
            raise RuntimeError(f"Runtime symlink must be root-owned: {part}")
        _check_machine_path(part.parent.resolve(), required=True)


def _shared_path(value: str, name: str, *, executable: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.exists():
        raise RuntimeError(
            f"Install {name} in a shared machine location before ug configure --global."
        )
    resolved = path.resolve()
    for candidate in (path, resolved):
        if any(
            candidate.is_relative_to(base)
            for base in (Path("/Users"), Path("/var/root"), Path("/private/var/root"))
        ):
            raise RuntimeError(
                f"{name} is installed in an administrator's home. Install it in a shared machine location first: {candidate}"
            )
    _check_machine_path(resolved, required=True)
    link = path
    for _ in range(40):
        _check_machine_links(link)
        _check_machine_path(link.parent.resolve(), required=True)
        for parent in link.parents:
            if not parent.stat().st_mode & stat.S_IXOTH:
                raise RuntimeError(f"{name} must be accessible to device users: {parent}")
        if not link.is_symlink():
            break
        target = link.readlink()
        link = target if target.is_absolute() else link.parent / target
    else:
        raise RuntimeError(f"Too many runtime symlinks for {name}: {path}")
    if not resolved.stat().st_mode & stat.S_IROTH:
        raise RuntimeError(f"{name} must be readable by device users: {resolved}")
    if (executable or resolved.is_dir()) and not resolved.stat().st_mode & stat.S_IXOTH:
        raise RuntimeError(f"{name} must be executable by device users: {resolved}")
    if executable and not path.is_file():
        raise RuntimeError(f"{name} must be an executable file: {path}")
    return path


def validate_cli_discovery() -> None:
    """Validate shared command lookup before CLI installation or version probes."""
    for directory in os.get_exec_path():
        _shared_path(directory, "MDM command PATH")
    for candidate in databricks._iter_databricks_executables():
        _shared_path(candidate, "Databricks CLI", executable=True)
    _shared_path(shutil.which("claude") or "", "Claude CLI", executable=True)
    databricks.clear_databricks_cli_cache()


def _runtime() -> dict[str, str]:
    validate_cli_discovery()
    python = _shared_path(os.path.abspath(sys.executable), "UG's Python runtime", executable=True)
    _shared_path(sys.prefix, "UG's Python environment")
    _shared_path(str(Path(__file__).resolve()), "UG package")
    probe = subprocess_cross_os.run(
        [str(python), "-I", "-c", "import ucode.agents.desktop.mdm as m; print(m.__file__)"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if probe.returncode or Path(probe.stdout.strip()).resolve() != Path(__file__).resolve():
        raise RuntimeError(
            "Install this UG package in the shared Python environment before MDM provisioning."
        )
    return {
        "python": str(python),
        "claude": str(_shared_path(shutil.which("claude") or "", "Claude CLI", executable=True)),
        "databricks": str(
            _shared_path(databricks.databricks_cli_path(), "Databricks CLI", executable=True)
        ),
    }


def _runtime_script(runtime: dict[str, str]) -> bytes:
    path = os.pathsep.join(
        dict.fromkeys(
            [
                str(MDM_ROOT),
                str(Path(runtime["databricks"]).parent),
                str(Path(runtime["claude"]).parent),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        )
    )
    # Runtime authentication belongs to the invoking user, never the provisioning environment.
    scrub = (
        "DATABRICKS_BEARER DATABRICKS_BEARER_COMMAND DATABRICKS_TOKEN "
        "DATABRICKS_CLIENT_ID DATABRICKS_CLIENT_SECRET DATABRICKS_AUTH_TYPE "
        "DATABRICKS_CONFIG_FILE DATABRICKS_CONFIG_PROFILE PYTHONPATH PYTHONHOME VIRTUAL_ENV"
    )
    return (
        f"#!/bin/sh\nunset {scrub}\nexport PATH={shlex.quote(path)}\n"
        f'exec {shlex.quote(runtime["python"])} -I -m ucode.cli "$@"\n'
    ).encode()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _policy_values(profile: dict) -> dict[str, str]:
    return {
        key: value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        for key, value in profile.items()
    }


def _read_policy() -> dict | None:
    """The installed policy, or None when it is unreadable, so it is never treated as UG's."""
    try:
        policy = plistlib.loads(POLICY_PATH.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return policy if isinstance(policy, dict) else None


def _publish(source: Path | None, files: dict[Path, bytes | None]) -> None:
    for path in files:
        _check_machine_path(path)
    if POLICY_PATH in files and POLICY_PATH.exists():
        if (_read_policy() or {}).get("inferenceCredentialHelper") != str(MDM_ROOT / "ug"):
            raise RuntimeError(
                "Existing Desktop policy is not this UG deployment; remove it through its MDM owner first."
            )
    system_path = managed_files.claude_desktop_org_plugin_path()
    if system_path is None:
        raise RuntimeError("Desktop MDM publication requires macOS.")
    _check_machine_path(system_path)
    try:
        with managed_files.managed_write_session():
            managed_files.replace_privileged_bundle(system_path, source)
            managed_files.reconcile_privileged_files(
                {
                    path: data.decode("utf-8") if data is not None else None
                    for path, data in files.items()
                },
                display="Claude Desktop",
            )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = str(getattr(exc, "stderr", None) or exc).strip()
        raise managed_files.ManagedFileWriteUnavailable(
            f"Could not update Desktop MDM files: {detail}"
        ) from exc


def _validate_selector(selector: dict) -> None:
    location = selector.get("unity_catalog_location")
    if location is not None and (
        not isinstance(location, str)
        or len(location.split(".")) != 2
        or any(not part.strip() or part != part.strip() for part in location.split("."))
    ):
        raise RuntimeError("MDM catalog locations must be catalog.schema.")
    names = selector.get("names", [])
    if not isinstance(names, list) or any(
        not isinstance(name, str)
        or len(name.split(".")) != 3
        or any(not part.strip() or part != part.strip() for part in name.split("."))
        for name in names
    ):
        raise RuntimeError(
            "MDM skill and MCP names must be fully qualified catalog.schema.name values."
        )


def _download_skills(workspace: str, token: str, selector: dict, destination: Path) -> list[dict]:
    _validate_selector(selector)
    refs, desired = _resolve_managed_skills(workspace, token, selector)
    if desired is None or {ref.fqn for ref in refs} != desired:
        raise RuntimeError(
            "Could not resolve all managed skills; the existing MDM installation is unchanged."
        )
    if len({ref.bundle_name for ref in refs}) != len(refs):
        raise RuntimeError(
            "Managed skills have duplicate bundle names; rename them before MDM deployment."
        )
    bundles = _fetch_bundles(workspace, token, refs, label="Fetching Desktop MDM skills")
    records = []
    for ref in refs:
        files, error = bundles.get(ref.fqn, (None, "missing response"))
        if error or not files:
            raise RuntimeError(
                f"Could not download managed skill {ref.fqn}; existing MDM installation is unchanged."
            )
        if Path(ref.bundle_name).name != ref.bundle_name or ref.bundle_name in (".", ".."):
            raise RuntimeError("Invalid managed skill bundle name.")
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in files):
            raise RuntimeError(f"Invalid paths in skill {ref.fqn}.")
        path = destination / ref.bundle_name
        _write_bundle(path, ref.bundle_name, files)
        records.append({"bundle_name": ref.bundle_name, "dirs": [str(path)]})
    return records


def require_dedicated_workspace(workspace: str) -> None:
    if split_workspace_key(workspace)[1] is not None or workspace_id_args(workspace):
        raise RuntimeError("Desktop MDM currently requires a dedicated workspace hostname.")


def configure(workspace: str, profile: str | None) -> None:
    validate_mdm_execution()
    if managed_files.managed_settings_skipped():
        raise RuntimeError(
            "Unset UG_SKIP_OS_SETTINGS and use global scope for Desktop MDM provisioning."
        )
    require_dedicated_workspace(workspace)
    if config_io.is_dry_run():
        print_note(f"Would publish Desktop policy to {POLICY_PATH} and a root organization plugin.")
        return
    runtime = _runtime()
    token = databricks.get_databricks_token(workspace, profile)
    fetched = get_managed_config(workspace, token)
    if fetched.reason and "feature_disabled" not in fetched.reason.lower():
        raise RuntimeError(f"Could not read workspace configuration: {fetched.reason}")
    managed = normalize_managed_config(fetched.manifest) if fetched.manifest is not None else None
    enabled_tools = managed_enabled_tools(managed or {})
    if enabled_tools and "claude" not in enabled_tools:
        raise RuntimeError("The workspace configuration does not enable Claude.")
    state = {"workspace": workspace, "profile": profile}
    parent_schema = None
    if managed is not None:
        state = resolve_state(managed, state, "claude")
        if not managed_provider_service(managed, "claude"):
            parent_schema = managed_unity_catalog_location(managed, "claude")
    settings = setup.resolve_gateway_profile(state, parent_schema=parent_schema, token=token)
    selector = (managed or {}).get("mcp_servers") or {}
    _validate_selector(selector)
    servers = _resolve_managed_mcp_servers(selector, workspace, profile, ["claude"], token=token)
    user_profile = "UG_DESKTOP_" + hashlib.sha256(workspace.encode()).hexdigest()[:12]
    helper = MDM_ROOT / "ug"
    helper_args = [
        "claude-desktop-auth",
        "--host",
        workspace,
        "--profile",
        user_profile,
    ]
    if state.get("claude_relayed"):
        helper_args += ["--relay", "--claude-binary", runtime["claude"]]
    rendered = claude_config.render_profile(
        **settings,
        helper_command=helper,
        helper_args=helper_args,
        otel_headers_helper=MDM_ROOT / "otel-headers.sh",
    )
    otel = (
        claude_config.render_otel_helper(
            [
                str(helper),
                "otel-headers",
                "--host",
                workspace,
                "--profile",
                user_profile,
            ],
            windows=False,
        ).encode()
        if settings["otel_endpoint"]
        else None
    )
    mcp_config = cowork._cowork_mcp_config(
        servers, workspace, user_profile, mdm_ug_executable=helper
    )
    rendered["managedMcpServers"] = claude_config.render_managed_mcp_servers(mcp_config)
    policy = plistlib.dumps(_policy_values(rendered))
    hooks = cowork._connector_hooks(mcp_config, workspace, user_profile, use_pat=False)
    with tempfile.TemporaryDirectory(prefix="ug-mdm-stage-") as directory:
        staging = Path(directory).resolve()
        records = _download_skills(
            workspace, token, (managed or {}).get("skills") or {}, staging / "skills"
        )
        source = staging / "marketplace"
        plugin_root = source / "plugins/ug"
        cowork._write_marketplace(
            source, plugin_root, records, list(mcp_config["mcpServers"]), hooks
        )
        manifest_path = plugin_root / ".claude-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["installationPreference"] = "required"
        manifest_path.write_bytes(_json_bytes(manifest))
        (plugin_root / "version.json").write_bytes(_json_bytes({"version": manifest["version"]}))
        _publish(
            plugin_root,
            {
                helper: _runtime_script(runtime),
                MDM_ROOT / "otel-headers.sh": otel,
                POLICY_PATH: policy,
            },
        )


def remove() -> bool:
    plugin = managed_files.claude_desktop_org_plugin_path()
    files: dict[Path, bytes | None] = {}
    if POLICY_PATH.exists():
        if (_read_policy() or {}).get("inferenceCredentialHelper") == str(MDM_ROOT / "ug"):
            files[POLICY_PATH] = None
    if not files and not MDM_ROOT.exists() and (plugin is None or not plugin.exists()):
        return False
    validate_mdm_execution()
    files.update({MDM_ROOT / "ug": None, MDM_ROOT / "otel-headers.sh": None})
    _publish(None, files)
    if MDM_ROOT.exists() and not any(MDM_ROOT.iterdir()):
        try:
            managed_files.remove_privileged_tree(MDM_ROOT, empty_only=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise managed_files.ManagedFileWriteUnavailable(
                f"Could not remove {MDM_ROOT}: {exc}"
            ) from exc
    return True
