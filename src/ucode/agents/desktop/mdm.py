"""Publish machine configuration and refresh each user's normal Cowork installation."""

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
from ucode.os_compatibility import subprocess_cross_os, system_cross_os
from ucode.skills_download import _fetch_bundles, _resolve_managed_skills, _write_bundle
from ucode.ui import print_note, print_warning
from ucode.workspace import split_workspace_key, workspace_id_args

MDM_ROOT = Path("/Library/Application Support/UnityGateway/claude-desktop-mdm")
POLICY_PATH = Path("/Library/Managed Preferences/com.anthropic.claudefordesktop.plist")
AGENT_LABEL = "com.databricks.unity-gateway.claude-desktop"
AGENT_PATH = Path("/Library/LaunchAgents") / f"{AGENT_LABEL}.plist"


def validate_mdm_execution(*, administrator: bool) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Desktop MDM currently supports macOS only.")
    if system_cross_os.is_admin() != administrator:
        role = "root from your MDM script" if administrator else "the logged-in user, not root"
        raise RuntimeError(
            f"Run Desktop MDM {'provisioning' if administrator else 'refresh'} as {role}."
        )


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
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError(f"Not a regular machine configuration path: {part}")


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
        _check_machine_path(link.parent, required=True)
        for parent in link.parents:
            if not parent.stat().st_mode & stat.S_IXOTH:
                raise RuntimeError(f"{name} must be accessible to device users: {parent}")
        if not link.is_symlink():
            _check_machine_path(link, required=True)
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
    """Validate root command lookup before CLI installation or version probes."""
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


def _machine_directory(path: Path) -> None:
    _check_machine_path(path)
    if path.exists():
        if not path.is_dir() or not path.stat().st_mode & stat.S_IXOTH:
            raise RuntimeError(f"Machine directory must be accessible to device users: {path}")
        return
    _machine_directory(path.parent)
    path.mkdir(mode=0o755)
    path.chmod(0o755)  # MDM scripts may run with a restrictive umask.


def _publish(source: Path | None, files: dict[Path, tuple[bytes | None, int]]) -> None:
    for path in files:
        _check_machine_path(path)
    if POLICY_PATH.exists():
        policy = plistlib.loads(POLICY_PATH.read_bytes())
        if policy.get("inferenceCredentialHelper") != str(MDM_ROOT / "ug"):
            raise RuntimeError(
                "Existing Desktop policy is not this UG deployment; remove it through its MDM owner first."
            )
    for path in files:
        _machine_directory(path.parent)
    system_path = managed_files.claude_desktop_marketplace_path()
    if system_path is None:
        raise RuntimeError("Desktop MDM publication requires macOS.")
    _check_machine_path(system_path)
    managed_files.replace_privileged_bundle(system_path, source)
    claude_config.write_configuration(
        [
            (path, claude_config.read_optional_bytes(path), data, mode)
            for path, (data, mode) in files.items()
        ]
    )


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
    validate_mdm_execution(administrator=True)
    if managed_files.managed_settings_skipped():
        raise RuntimeError(
            "Unset UG_SKIP_OS_SETTINGS and use global scope for Desktop MDM provisioning."
        )
    require_dedicated_workspace(workspace)
    if config_io.is_dry_run():
        print_note(
            f"Would publish Desktop policy to {POLICY_PATH}, a root marketplace, and {AGENT_PATH}."
        )
        return
    runtime = _runtime()
    try:
        token = databricks.get_databricks_token(workspace, profile, browser_login=False)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Desktop MDM provisioning needs an existing Databricks credential; "
            f"authenticate before running the MDM command: {exc}"
        ) from exc
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
    policy = plistlib.dumps(_policy_values(rendered))
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
    hooks = cowork._connector_hooks(mcp_config, workspace, user_profile, use_pat=False)
    with tempfile.TemporaryDirectory(prefix="ug-mdm-stage-") as directory:
        staging = Path(directory).resolve()
        records = _download_skills(
            workspace, token, (managed or {}).get("skills") or {}, staging / "skills"
        )
        source = staging / "marketplace"
        cowork._write_marketplace(source, source / "plugins/ug", records, mcp_config, hooks)
        configuration = {
            "version": 1,
            "workspace": workspace,
            "profile": user_profile,
            "runtime": runtime,
        }
        agent = plistlib.dumps(
            {
                "Label": AGENT_LABEL,
                "ProgramArguments": [str(helper), "desktop-mdm-sync"],
                "RunAtLoad": True,
                "StartInterval": 60,
                "ProcessType": "Background",
            }
        )
        _publish(
            source,
            {
                helper: (_runtime_script(runtime), 0o755),
                MDM_ROOT / "otel-headers.sh": (otel, 0o755),
                MDM_ROOT / "deployment.json": (_json_bytes(configuration), 0o644),
                AGENT_PATH: (agent, 0o644),
                POLICY_PATH: (policy, 0o644),
            },
        )
    _activate_logged_in_user()


def _activate_logged_in_user() -> None:
    try:
        uid = Path("/dev/console").stat().st_uid
        if uid < 500:
            return
        subprocess_cross_os.run(
            ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(AGENT_PATH)],
            capture_output=True,
            check=False,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        kicked = subprocess_cross_os.run(
            ["/bin/launchctl", "kickstart", f"gui/{uid}/{AGENT_LABEL}"],
            capture_output=True,
            check=False,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if kicked.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    print_warning(
        "Desktop refresh will start at the next macOS login; the current user can run `ug desktop-mdm-sync` now."
    )


def remove() -> None:
    validate_mdm_execution(administrator=True)
    if not MDM_ROOT.exists() and not POLICY_PATH.exists():
        return
    _publish(
        None,
        {
            POLICY_PATH: (None, 0o644),
            AGENT_PATH: (None, 0o644),
            MDM_ROOT / "ug": (None, 0o755),
            MDM_ROOT / "otel-headers.sh": (None, 0o755),
            MDM_ROOT / "deployment.json": (None, 0o644),
        },
    )
    if not any(MDM_ROOT.iterdir()):
        MDM_ROOT.rmdir()


def _cached_version(cowork_root: Path) -> str | None:
    registry = config_io.read_json_safe(cowork_root / "cowork_plugins/installed_plugins.json")
    plugins = registry.get("plugins")
    if not isinstance(plugins, dict):
        return None
    entries = plugins.get(cowork.PLUGIN_ID, [])
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        return None
    install_path = entries[0].get("installPath")
    if not isinstance(install_path, str) or not install_path:
        return None
    plugin = Path(install_path)
    return config_io.read_json_safe(plugin / ".claude-plugin/plugin.json").get("version")


def sync_user() -> bool:
    validate_mdm_execution(administrator=False)
    path = MDM_ROOT / "deployment.json"
    _check_machine_path(path, required=True)
    configuration = json.loads(path.read_bytes())
    if configuration.get("version") != 1:
        raise RuntimeError("Unsupported Desktop MDM deployment version; update UG.")
    if not cowork._cowork_roots():
        return False
    system_source = managed_files.claude_desktop_marketplace_path()
    if system_source is None:
        raise RuntimeError("Desktop MDM refresh requires macOS.")
    _check_machine_path(system_source, required=True)
    root = cowork.discover_cowork_root()
    source = cowork._registered_marketplace_source(root)
    if source is not None and source not in (cowork._marketplace_root(), system_source):
        raise RuntimeError("UG marketplace is registered to another source; left unchanged.")
    org_plugin = managed_files.claude_desktop_org_plugin_path()
    if org_plugin is not None and (org_plugin.exists() or org_plugin.is_symlink()):
        raise RuntimeError("Remove the prior organization-loader UG plugin before MDM refresh.")
    version = json.loads((system_source / "plugins/ug/.claude-plugin/plugin.json").read_bytes())[
        "version"
    ]
    if source == system_source and _cached_version(root) == version:
        return False
    cowork._install_plugin(
        root, system_source, mdm_claude_binary=configuration["runtime"]["claude"]
    )
    if _cached_version(root) != version:
        raise RuntimeError(
            "Desktop plugin refresh did not install the published version; retry refresh."
        )
    return True
