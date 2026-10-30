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
from ucode.agents.desktop import claude_config, cowork, setup, system_marketplace
from ucode.managed_config import get_managed_config
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
from ucode.workspace import split_workspace_key

MDM_ROOT = Path("/Library/Application Support/UnityGateway/claude-desktop-mdm")
POLICY_PATH = Path("/Library/Managed Preferences/com.anthropic.claudefordesktop.plist")
AGENT_LABEL = "com.databricks.unity-gateway.claude-desktop"
AGENT_PATH = Path("/Library/LaunchAgents") / f"{AGENT_LABEL}.plist"
USER_STATE_FILENAME = "claude-desktop-mdm.json"


def _require_macos(*, administrator: bool) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("--mdm-desktop currently supports macOS only.")
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
        raise RuntimeError(f"Install {name} in a shared machine location before --mdm-desktop.")
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
    _check_machine_path(path.parent, required=True)
    for parent in (path.parent, *path.parent.parents, *resolved.parents):
        if not parent.stat().st_mode & stat.S_IXOTH:
            raise RuntimeError(f"{name} must be accessible to device users: {parent}")
    if not resolved.stat().st_mode & stat.S_IROTH:
        raise RuntimeError(f"{name} must be readable by device users: {resolved}")
    if (executable or resolved.is_dir()) and not resolved.stat().st_mode & stat.S_IXOTH:
        raise RuntimeError(f"{name} must be executable by device users: {resolved}")
    if executable and not path.is_file():
        raise RuntimeError(f"{name} must be an executable file: {path}")
    return path


def _runtime() -> dict[str, str]:
    python = _shared_path(os.path.abspath(sys.executable), "UG's Python runtime", executable=True)
    _shared_path(sys.prefix, "UG's Python environment")
    _shared_path(str(Path(__file__).resolve()), "UG package")
    probe = subprocess_cross_os.run(
        [str(python), "-I", "-c", "import ucode.agents.desktop.mdm as m; print(m.__file__)"],
        capture_output=True,
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


def _read_machine_state() -> dict:
    path = MDM_ROOT / "state.json"
    _check_machine_path(path)
    return json.loads(path.read_bytes()) if path.exists() else {}


def _owned_file(path: Path, state: dict) -> bytes | None:
    _check_machine_path(path)
    before = claude_config.read_optional_bytes(path)
    if before is not None and hashlib.sha256(before).hexdigest() != state.get("files", {}).get(
        str(path)
    ):
        raise RuntimeError(
            f"Existing file is not this MDM installation or was edited externally: {path}. Remove the previous policy through its owner first."
        )
    return before


def _machine_directory(path: Path) -> None:
    _check_machine_path(path)
    if path.exists():
        if not path.is_dir() or not path.stat().st_mode & stat.S_IXOTH:
            raise RuntimeError(f"Machine directory must be accessible to device users: {path}")
        return
    _machine_directory(path.parent)
    path.mkdir(mode=0o755)
    path.chmod(0o755)  # MDM scripts may run with a restrictive umask.


def _publish(
    source: Path | None, files: dict[Path, tuple[bytes | None, int]], configuration: dict
) -> None:
    state_path = MDM_ROOT / "state.json"
    previous = _read_machine_state()
    changes = [
        (path, _owned_file(path, previous), data, mode) for path, (data, mode) in files.items()
    ]
    for path in files:
        _machine_directory(path.parent)
    system_path = managed_files.claude_desktop_marketplace_path()
    assert system_path is not None
    _check_machine_path(system_path)
    system_marketplace.owned_bundle(system_path, previous)
    state_before = claude_config.read_optional_bytes(state_path)
    backup = Path(tempfile.mkdtemp(prefix="ug-mdm-rollback-")).resolve()
    (backup / "files.json").write_bytes(
        _json_bytes(
            {
                str(path): {
                    "contents_hex": data.hex() if data is not None else None,
                    "mode": stat.S_IMODE(path.stat().st_mode) if data is not None else None,
                }
                for path, data, _, _ in changes
            }
        )
    )
    if state_before is not None:
        (backup / "state.json").write_bytes(state_before)
    retain_backup = False
    try:
        if system_path.exists():
            shutil.copytree(system_path, backup / "marketplace")
        try:
            if source is None:
                system_marketplace.remove(state_path)
            else:
                system_marketplace.install(source, state_path)
            state = _read_machine_state()
            owned = dict(previous.get("files", {}))
            for path, (data, _) in files.items():
                if data is None:
                    owned.pop(str(path), None)
                else:
                    owned[str(path)] = hashlib.sha256(data).hexdigest()
            state.update(files=owned, configuration=configuration)
            claude_config.write_configuration(
                [
                    *changes,
                    (
                        state_path,
                        claude_config.read_optional_bytes(state_path),
                        _json_bytes(state),
                        0o600,
                    ),
                ]
            )
        except Exception as exc:
            try:
                retain_backup = any(
                    claude_config.read_optional_bytes(path) != before
                    for path, before, _, _ in changes
                )
            except OSError:
                retain_backup = True
            try:
                if (backup / "marketplace").exists():
                    system_marketplace.install(backup / "marketplace", state_path)
                else:
                    system_marketplace.remove(state_path)
                claude_config.write_configuration(
                    [
                        (
                            state_path,
                            claude_config.read_optional_bytes(state_path),
                            state_before,
                            0o600,
                        )
                    ]
                )
            except Exception as rollback_error:
                retain_backup = True
                raise RuntimeError(
                    f"MDM publication failed ({exc}); rollback failed ({rollback_error}). Recovery source retained at {backup}."
                ) from rollback_error
            if retain_backup:
                raise RuntimeError(f"{exc}. Recovery files retained at {backup}.") from exc
            raise
    finally:
        if not retain_backup:
            shutil.rmtree(backup)


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


def configure(workspace: str, profile: str | None, *, mcp_names: list[str] | None = None) -> None:
    _require_macos(administrator=True)
    if managed_files.managed_settings_skipped():
        raise RuntimeError(
            "Unset UG_SKIP_OS_SETTINGS and use global scope for Desktop MDM provisioning."
        )
    if split_workspace_key(workspace)[1] is not None:
        raise RuntimeError("Desktop MDM currently requires a dedicated workspace hostname.")
    if config_io.is_dry_run():
        print_note(
            f"Would publish Desktop policy to {POLICY_PATH}, a root marketplace, and {AGENT_PATH}."
        )
        return
    runtime = _runtime()
    _machine_directory(MDM_ROOT)
    _check_machine_path(MDM_ROOT / ".lock")
    with claude_config.locked(MDM_ROOT / ".lock"):
        token = databricks.get_databricks_token(workspace, profile, browser_login=True)
        fetched = get_managed_config(workspace, token)
        if fetched.reason:
            raise RuntimeError(f"Could not read workspace configuration: {fetched.reason}")
        managed = fetched.manifest
        if managed is not None and "claude" not in managed_enabled_tools(managed):
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
        servers = _resolve_managed_mcp_servers(
            selector, workspace, profile, ["claude"], token=token
        )
        if mcp_names is not None:
            mcp_names = [name.strip() for name in mcp_names]
            _validate_selector({"names": mcp_names})
            servers += _resolve_managed_mcp_servers(
                {"names": mcp_names}, workspace, profile, ["claude"]
            )
        user_profile = "UG_DESKTOP_" + hashlib.sha256(workspace.encode()).hexdigest()[:12]
        helper = MDM_ROOT / "ug"
        helper_args = [
            "claude-desktop-auth",
            "--host",
            workspace,
            "--profile",
            user_profile,
            "--mdm-desktop",
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
        mcp_config = cowork._cowork_mcp_config(servers, workspace, user_profile, executable=helper)
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
                "enabled": True,
                "workspace": workspace,
                "profile": user_profile,
                "runtime": runtime,
                "source_digest": system_marketplace.bundle_digest(
                    system_marketplace.read_bundle(source) or {}
                ),
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
                configuration,
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
    _require_macos(administrator=True)
    _check_machine_path(MDM_ROOT)
    if not (MDM_ROOT / "state.json").exists():
        return
    _check_machine_path(MDM_ROOT / ".lock")
    with claude_config.locked(MDM_ROOT / ".lock"):
        previous = _read_machine_state()
        configuration = {**previous["configuration"], "enabled": False}
        _publish(
            None,
            {
                POLICY_PATH: (None, 0o644),
                MDM_ROOT / "otel-headers.sh": (None, 0o755),
                MDM_ROOT / "deployment.json": (_json_bytes(configuration), 0o644),
            },
            configuration,
        )
    _activate_logged_in_user()


def _cached_version(cowork_root: Path) -> str | None:
    registry = config_io.read_json_safe(cowork_root / "cowork_plugins/installed_plugins.json")
    entries = registry.get("plugins", {}).get(cowork.PLUGIN_ID, [])
    if not isinstance(entries, list) or len(entries) != 1 or not entries[0].get("installPath"):
        return None
    plugin = Path(entries[0]["installPath"])
    return config_io.read_json_safe(plugin / ".claude-plugin/plugin.json").get("version")


def _cleanup_registrations(
    registrations: dict, source: Path, claude: str, *, keep: Path | None = None
) -> None:
    for directory, versions in registrations.items():
        root = Path(directory)
        if root == keep:
            continue
        version = _cached_version(root)
        if cowork._registered_marketplace_source(root) == source and (
            version in versions or version is None
        ):
            cowork._remove_plugin_registration(root, claude=claude)


def sync_user() -> bool:
    _require_macos(administrator=False)
    path = MDM_ROOT / "deployment.json"
    _check_machine_path(path, required=True)
    configuration = json.loads(path.read_bytes())
    if configuration.get("version") != 1:
        raise RuntimeError("Unsupported Desktop MDM deployment version; update UG.")
    system_source = managed_files.claude_desktop_marketplace_path()
    assert system_source is not None
    claude = configuration["runtime"]["claude"]
    user_state = Path.home() / ".ucode" / USER_STATE_FILENAME
    with cowork._locked():
        previous = config_io.read_json_safe(user_state)
        registrations = previous.get("registrations", {})
        if not configuration["enabled"]:
            _cleanup_registrations(registrations, system_source, claude)
            user_state.unlink(missing_ok=True)
            return False
        if not cowork._cowork_roots():
            return False
        _check_machine_path(system_source, required=True)
        bundle = system_marketplace.read_bundle(system_source)
        if (
            bundle is None
            or system_marketplace.bundle_digest(bundle) != configuration["source_digest"]
        ):
            return False  # Publication may be between the source swap and deployment record.
        root = cowork.discover_cowork_root()
        source = cowork._registered_marketplace_source(root)
        if source is not None and source not in (cowork._marketplace_root(), system_source):
            raise RuntimeError("UG marketplace is registered to another source; left unchanged.")
        org_plugin = managed_files.claude_desktop_org_plugin_path()
        if org_plugin is not None and (org_plugin.exists() or org_plugin.is_symlink()):
            raise RuntimeError("Remove the prior organization-loader UG plugin before MDM refresh.")
        version = json.loads(
            (system_source / "plugins/ug/.claude-plugin/plugin.json").read_bytes()
        )["version"]
        current = (
            previous.get("cowork_root") == str(root)
            and previous.get("source_digest") == configuration["source_digest"]
            and source == system_source
            and _cached_version(root) == version
        )
        if not current:
            # Record the intended version before native commands so an interrupted refresh is removable.
            registrations[str(root)] = sorted({*registrations.get(str(root), []), version})
            config_io.atomic_write_json(user_state, {**previous, "registrations": registrations})
            cowork._install_plugin(root, system_source, claude=claude)
            if _cached_version(root) != version:
                raise RuntimeError(
                    "Desktop plugin refresh did not install the published version; retry refresh."
                )
        _cleanup_registrations(registrations, system_source, claude, keep=root)
        updated = {
            "cowork_root": str(root),
            "source_digest": configuration["source_digest"],
            "registrations": {str(root): [version]},
        }
        if updated != previous:
            config_io.atomic_write_json(user_state, updated)
        return not current
