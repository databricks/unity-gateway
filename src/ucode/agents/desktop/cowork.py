"""Install Unity Gateway workspace plugins into Claude Cowork and Code."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

from ucode import config_io, managed_files
from ucode.agents.desktop.claude import claude_desktop_root_directory, desktop_mcp_configuration
from ucode.databricks import build_mcp_proxy_argv
from ucode.mcp_connection_login import connection_from_url
from ucode.os_compatibility import subprocess_cross_os
from ucode.skills_state import records_for_scope
from ucode.ui import print_warning
from ucode.workspace import workspace_id_args

MARKETPLACE_NAME = "unity-gateway-cowork"
PLUGIN_NAME = "ug"
PLUGIN_DISPLAY_NAME = "ug"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
LEGACY_PLUGIN_ID = f"unity-gateway@{MARKETPLACE_NAME}"
STATE_FILENAME = "claude-cowork.json"
MARKETPLACE_DIRECTORY_NAME = "claude-cowork-marketplace"
CONNECTOR_SKILL_NAME = "unity-gateway-connectors"
SESSION_START_HOOK_TIMEOUT_SECONDS = 30
PROMPT_HOOK_TIMEOUT_SECONDS = 20
CODE_MARKETPLACE_NAME = "unity-gateway-code"
CODE_PLUGIN_ID = f"{PLUGIN_NAME}@{CODE_MARKETPLACE_NAME}"
CODE_MARKETPLACE_DIRECTORY_NAME = "claude-code-marketplace"
CODE_STATE_FILENAME = "claude-code-plugin.json"

_COWORK_MARKERS = (
    "cowork_settings.json",
    "cowork_plugins",
    "cowork_plugins/known_marketplaces.json",
    "cowork_plugins/installed_plugins.json",
)


def configure_code(mcp_servers: list[dict]) -> bool:
    """Install connector guidance alongside Code's existing MCP registrations."""
    server_names = list(
        dict.fromkeys(
            server["name"]
            for server in mcp_servers
            if "claude" in (server.get("clients") or [])
            and isinstance(server.get("name"), str)
            and server["name"]
            and isinstance(server.get("url"), str)
            and server["url"]
        )
    )
    marketplace_root = config_io.APP_DIR / CODE_MARKETPLACE_DIRECTORY_NAME
    state_path = config_io.APP_DIR / CODE_STATE_FILENAME
    previous_state = config_io.read_json_safe(state_path)
    if not server_names and not previous_state and not marketplace_root.exists():
        return False
    config_root = _code_config_root()
    previous_root = previous_state.get("config_root")
    _write_marketplace(
        marketplace_root,
        marketplace_root / "plugins" / PLUGIN_NAME,
        [],
        server_names,
        marketplace_name=CODE_MARKETPLACE_NAME,
        description="Unity Gateway connector guidance for Claude Code",
        code=True,
    )
    _install_plugin(
        config_root,
        marketplace_root,
        cowork=False,
        marketplace_name=CODE_MARKETPLACE_NAME,
        plugin_id=CODE_PLUGIN_ID,
    )
    if isinstance(previous_root, str) and previous_root and Path(previous_root) != config_root:
        _remove_plugin_registration(
            Path(previous_root),
            cowork=False,
            marketplace_name=CODE_MARKETPLACE_NAME,
            plugin_ids=(CODE_PLUGIN_ID,),
        )
    config_io.atomic_write_json(state_path, {"version": 1, "config_root": str(config_root)})
    return True


def revert_code() -> bool:
    marketplace_root = config_io.APP_DIR / CODE_MARKETPLACE_DIRECTORY_NAME
    state_path = config_io.APP_DIR / CODE_STATE_FILENAME
    state = config_io.read_json_safe(state_path)
    if not state and not marketplace_root.exists() and not marketplace_root.is_symlink():
        return False
    raw_root = state.get("config_root")
    config_root = Path(raw_root) if isinstance(raw_root, str) and raw_root else _code_config_root()
    _remove_plugin_registration(
        config_root,
        cowork=False,
        marketplace_name=CODE_MARKETPLACE_NAME,
        plugin_ids=(CODE_PLUGIN_ID,),
    )
    if marketplace_root.is_symlink():
        marketplace_root.unlink()
    elif marketplace_root.exists():
        shutil.rmtree(marketplace_root)
    state_path.unlink(missing_ok=True)
    return True


def _code_config_root() -> Path:
    return (
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().absolute()
    )


def configure_cowork(state: dict, mcp_servers: list[dict], *, sync_plugin: bool = True) -> bool:
    """Register native connectors and optionally refresh the workspace plugin."""
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("No Databricks workspace is configured for Cowork.")

    previous_state = config_io.read_json_safe(_state_path())
    skill_records = _workspace_skill_records(workspace) if sync_plugin else []
    org_plugin = managed_files.claude_desktop_org_plugin_path()
    if org_plugin is not None and (org_plugin.exists() or org_plugin.is_symlink()):
        raise RuntimeError(
            f"An organization plugin already exists at {org_plugin}. Remove the previous UG "
            "pilot, or ask its administrator to remove it, before installing the marketplace plugin."
        )

    profile = state.get("profile")
    if not isinstance(profile, str) or not profile:
        if not previous_state and not skill_records and not mcp_servers:
            return False
        raise RuntimeError("Select an explicit Databricks CLI profile for Cowork.")

    mcp_config = _cowork_mcp_config(
        mcp_servers,
        workspace,
        profile,
        use_pat=bool(state.get("use_pat")),
        custom_oauth=bool(state.get("custom_oauth")),
    )
    with desktop_mcp_configuration(workspace, mcp_config):
        if not sync_plugin:
            return False
        if not previous_state and not skill_records and not mcp_config["mcpServers"]:
            return False
        if not previous_state and not _cowork_sessions_root_exists():
            return False

        cowork_root = discover_cowork_root()
        previous_root = previous_state.get("cowork_root")
        if (
            isinstance(previous_root, str)
            and previous_root
            and Path(previous_root).resolve() != cowork_root.resolve()
        ):
            _remove_plugin_registration(Path(previous_root))
        marketplace_root = _marketplace_root()
        plugin_root = marketplace_root / "plugins" / PLUGIN_NAME
        hooks = _connector_hooks(mcp_config, workspace, profile, use_pat=bool(state.get("use_pat")))
        _write_marketplace(
            marketplace_root,
            plugin_root,
            skill_records,
            list(mcp_config["mcpServers"]),
            hooks,
        )
        config_io.atomic_write_json(
            _state_path(),
            {"version": 1, "cowork_root": str(cowork_root)},
        )
        _install_plugin(cowork_root, marketplace_root)
    return True


def revert_cowork() -> bool:
    """Remove only UG's Cowork plugin, marketplace registration, and generated source."""
    if config_io.is_dry_run():
        return False
    state_path = _state_path()
    state = config_io.read_json_safe(state_path)
    raw_root = state.get("cowork_root")
    roots: list[Path] = []
    if isinstance(raw_root, str) and raw_root:
        roots.append(Path(raw_root))
    for root in _cowork_roots():
        if root not in roots and _has_ug_registration(root):
            roots.append(root)

    changed = False
    for root in roots:
        changed = _remove_plugin_registration(root) or changed
    marketplace_root = _marketplace_root()
    if marketplace_root.exists() or marketplace_root.is_symlink():
        if marketplace_root.is_symlink():
            marketplace_root.unlink()
        else:
            shutil.rmtree(marketplace_root)
        changed = True
    if state_path.exists():
        state_path.unlink()
        changed = True
    return changed


def _cowork_sessions_root_exists() -> bool:
    root = claude_desktop_root_directory()
    return root is not None and (root / "local-agent-mode-sessions").is_dir()


def _remove_plugin_registration(
    config_root: Path,
    *,
    cowork: bool = True,
    marketplace_name: str = MARKETPLACE_NAME,
    plugin_ids: tuple[str, ...] = (PLUGIN_ID, LEGACY_PLUGIN_ID),
) -> bool:
    """Remove only UG's plugin and marketplace from the selected scope."""

    claude = _claude_binary()
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_root)
    scope = ["--cowork"] if cowork else []
    plugin_result = False
    for plugin_id in plugin_ids:
        result = _run_claude_json(
            claude,
            ["plugin", "uninstall", plugin_id, *scope, "--json"],
            env,
            accepted_failures=("not installed", "not_installed", "not found"),
        )
        plugin_result = bool(result) or plugin_result
    marketplace_result = _run_claude_json(
        claude,
        [
            "plugin",
            "marketplace",
            "remove",
            marketplace_name,
            *scope,
            "--json",
        ],
        env,
        accepted_failures=("not found", "not_found", "not registered"),
    )
    return bool(plugin_result or marketplace_result)


def discover_cowork_root(*, desktop_root: Path | None = None) -> Path:
    """Find the active account/org Cowork configuration root without guessing IDs."""

    root = desktop_root or claude_desktop_root_directory()
    if root is None:
        raise RuntimeError("Claude Cowork configuration is unavailable on this platform.")
    candidates = _cowork_roots(desktop_root=root)
    if not candidates:
        raise RuntimeError("Open and start Claude Cowork once, then run `ug configure` again.")
    if len(candidates) == 1:
        return candidates[0]

    mtimes = {candidate: _cowork_mtime(candidate) for candidate in candidates}
    newest = max(mtimes.values())
    winners = [candidate for candidate, mtime in mtimes.items() if mtime == newest]
    if len(winners) == 1:
        return winners[0]
    marked_winners = [candidate for candidate in winners if _has_cowork_marker(candidate)]
    if len(marked_winners) == 1:
        return marked_winners[0]
    paths = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"Could not determine the active Cowork profile; candidates: {paths}.")


def _cowork_roots(*, desktop_root: Path | None = None) -> list[Path]:
    root = desktop_root or claude_desktop_root_directory()
    if root is None:
        return []
    sessions_root = root / "local-agent-mode-sessions"
    return sorted(
        grandchild for child in _directories(sessions_root) for grandchild in _directories(child)
    )


def _has_ug_registration(cowork_root: Path) -> bool:
    for relative_path in (
        "cowork_plugins/installed_plugins.json",
        "cowork_plugins/known_marketplaces.json",
    ):
        try:
            content = (cowork_root / relative_path).read_text(encoding="utf-8")
        except OSError:
            continue
        if PLUGIN_ID in content or MARKETPLACE_NAME in content:
            return True
    return False


def _directories(path: Path) -> list[Path]:
    try:
        return [entry for entry in path.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _has_cowork_marker(candidate: Path) -> bool:
    return any((candidate / marker).exists() for marker in _COWORK_MARKERS)


def _cowork_mtime(candidate: Path) -> int:
    mtimes: list[int] = []
    for path in (candidate, *(candidate / marker for marker in _COWORK_MARKERS)):
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes, default=0)


def _workspace_skill_records(workspace: str) -> list[dict]:
    normalized_workspace = workspace.rstrip("/")
    return [
        record
        for scope in ("managed", "user")
        for record in records_for_scope(scope, str(Path.home()))
        if str(record.get("workspace") or "").rstrip("/") == normalized_workspace
    ]


def _cowork_mcp_config(
    servers: list[dict],
    workspace: str,
    profile: str,
    *,
    use_pat: bool = False,
    custom_oauth: bool = False,
    mdm_ug_executable: Path | None = None,
) -> dict:
    entries: dict[str, dict] = {}
    urls: dict[str, str] = {}
    for server in servers:
        name = server.get("name")
        url = server.get("url")
        clients = server.get("clients") or []
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            continue
        if "claude" not in clients:
            continue
        if custom_oauth:
            print_warning(f"Skipping Cowork MCP server `{name}`: custom OAuth is not supported.")
            continue
        argv = build_mcp_proxy_argv(
            url,
            workspace,
            profile,
            use_pat=use_pat,
        )
        command = mdm_ug_executable if mdm_ug_executable is not None else Path(argv[0])
        if not command.is_absolute():
            raise RuntimeError(
                "Could not resolve the absolute `ug` executable required by Claude Cowork."
            )
        entries[name] = {"type": "stdio", "command": str(command), "args": argv[1:]}
        urls[name] = url
    aliases = _cowork_mcp_aliases(urls)
    return {"mcpServers": {aliases[name]: entry for name, entry in entries.items()}}


def _cowork_mcp_aliases(urls: dict[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for name, url in urls.items():
        full_name = unquote(connection_from_url(url) or "")
        parts = full_name.split(".")
        aliases[name] = (
            parts[-1]
            if len(parts) == 3 and all(parts) and name == full_name.replace(".", "-")
            else name
        )

    counts = Counter(aliases.values())
    used = set(aliases.values())  # Reserve custom names before allocating qualifiers.
    for name in sorted(aliases):
        alias = aliases[name]
        if alias == name or counts[alias] == 1:
            continue
        qualified = f"{alias}-{name.removesuffix(f'-{alias}')}"
        candidate = qualified
        suffix = 2
        while candidate in used:
            candidate = f"{qualified}-{suffix}"
            suffix += 1
        aliases[name] = candidate
        used.add(candidate)
    return aliases


def _marketplace_files(root: Path) -> dict[str, tuple[bytes, int]] | None:
    if not root.exists():
        return None
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            0o755 if path.stat().st_mode & 0o111 else 0o644,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _marketplace_digest(files: dict[str, tuple[bytes, int]]) -> str:
    digest = hashlib.sha256()
    for name, (content, mode) in sorted(files.items()):
        digest.update(json.dumps([name, mode, len(content)]).encode("utf-8"))
        digest.update(content)
    return digest.hexdigest()


def _write_marketplace(
    marketplace_root: Path,
    plugin_root: Path,
    skill_records: list[dict],
    connector_names: list[str],
    hooks: dict | None = None,
    *,
    marketplace_name: str = MARKETPLACE_NAME,
    description: str = "Unity Gateway workspace skills and connector guidance for Cowork",
    code: bool = False,
) -> None:
    marketplace_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{marketplace_root.name}-", dir=marketplace_root.parent)
    )
    backup = marketplace_root.with_name(f".{marketplace_root.name}.previous")
    try:
        staging_plugin = staging / plugin_root.relative_to(marketplace_root)
        _write_marketplace_contents(
            staging,
            staging_plugin,
            skill_records,
            connector_names,
            hooks,
            marketplace_name=marketplace_name,
            description=description,
            code=code,
        )
        if backup.exists():
            shutil.rmtree(backup)
        if marketplace_root.exists():
            os.replace(marketplace_root, backup)
        try:
            os.replace(staging, marketplace_root)
        except OSError:
            if backup.exists() and not marketplace_root.exists():
                os.replace(backup, marketplace_root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _write_marketplace_contents(
    marketplace_root: Path,
    plugin_root: Path,
    skill_records: list[dict],
    connector_names: list[str],
    hooks: dict | None,
    *,
    marketplace_name: str = MARKETPLACE_NAME,
    description: str = "Unity Gateway workspace skills and connector guidance for Cowork",
    code: bool = False,
) -> None:
    (marketplace_root / ".claude-plugin").mkdir(parents=True)
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    marketplace: dict[str, object] = {
        "name": marketplace_name,
        "owner": {"name": "Databricks"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "displayName": PLUGIN_DISPLAY_NAME,
                "description": description,
                "source": f"./plugins/{PLUGIN_NAME}",
            }
        ],
    }
    plugin: dict[str, object] = {
        "name": PLUGIN_NAME,
        "displayName": PLUGIN_DISPLAY_NAME,
        "description": description,
        "author": {"name": "Databricks"},
    }
    if hooks:
        _write_json(plugin_root / "hooks" / "hooks.json", hooks)
    _write_json(marketplace_root / ".claude-plugin" / "marketplace.json", marketplace)
    _write_plugin_skills(plugin_root / "skills", skill_records, connector_names, code=code)
    if not code:
        files = _marketplace_files(plugin_root)
        if files is None:
            raise RuntimeError("Generated plugin directory is missing.")
        files[".claude-plugin/plugin.json"] = (_json_bytes(plugin), 0o644)
        plugin["version"] = "0.0.0-ug-" + _marketplace_digest(files)
    _write_json(plugin_root / ".claude-plugin" / "plugin.json", plugin)


def _write_plugin_skills(
    destination: Path, skill_records: list[dict], server_names: list[str], *, code: bool = False
) -> None:
    _copy_skills(destination, skill_records)
    if server_names:
        _write_connector_skill(destination, server_names, code=code)


def _copy_skills(destination: Path, records: list[dict]) -> None:
    copied: set[str] = set()
    for record in records:
        bundle_name = record.get("bundle_name")
        if not isinstance(bundle_name, str) or not bundle_name or bundle_name in copied:
            continue
        source = _claude_skill_directory(record)
        if source is None:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination / bundle_name)
        copied.add(bundle_name)


def _connector_hooks(
    mcp_config: dict, workspace: str, profile: str, *, use_pat: bool
) -> dict | None:
    # Reuse each proxy entry's absolute `ug` and URL so the hook targets exactly what Cowork runs.
    urls: dict[str, str] = {}
    command = None
    for name, entry in mcp_config["mcpServers"].items():
        args = entry.get("args") or []
        if "--url" in args[:-1]:
            urls[name] = args[args.index("--url") + 1]
            command = entry["command"]
    if command is None:
        return None
    argv = [command, "cowork-hook", "--host", workspace.rstrip("/"), *workspace_id_args(workspace)]
    argv += ["--profile", profile]
    if use_pat:
        argv.append("--use-pat")
    for name, url in urls.items():
        argv += ["--server", f"{name}={url}"]
    command_line = subprocess_cross_os.shell_command(argv)
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command_line, "timeout": timeout}]}]
            for event, timeout in (
                ("SessionStart", SESSION_START_HOOK_TIMEOUT_SECONDS),
                ("UserPromptSubmit", PROMPT_HOOK_TIMEOUT_SECONDS),
            )
        }
    }


def _write_connector_skill(
    destination: Path, server_names: list[str], *, code: bool = False
) -> None:
    skill_dir = destination / CONNECTOR_SKILL_NAME
    if skill_dir.exists():
        return
    scope = "code" if code else "cowork"
    template = (Path(__file__).with_name("connector_skills") / f"{scope}.md").read_text(
        encoding="utf-8"
    )
    frontmatter, guidance = template.removeprefix("---\n").split("\n---\n", 1)
    description_line = next(
        line for line in frontmatter.splitlines() if line.startswith("description: ")
    )
    prefix, suffix = json.loads(description_line.removeprefix("description: ")).split(
        "{connectors}"
    )
    visible = []
    # Skill descriptions have a 1,024-character limit; the full list stays in the body.
    for name in server_names:
        candidate = ", ".join([*visible, f"`{name}`"])
        if len(prefix + candidate + suffix) > 1024:
            break
        visible.append(f"`{name}`")
    description = prefix + (", ".join(visible) or "configured workspace services") + suffix
    connectors = ", ".join(f"`{name}`" for name in server_names)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {CONNECTOR_SKILL_NAME}\ndescription: {json.dumps(description)}\n---\n"
        + guidance.replace("{connectors}", connectors),
        encoding="utf-8",
    )


def _claude_skill_directory(record: dict) -> Path | None:
    existing = [Path(raw) for raw in (record.get("dirs") or []) if Path(raw).is_dir()]
    return next(
        (
            path
            for path in existing
            if path.parent.name == "skills" and path.parent.parent.name == ".claude"
        ),
        existing[0] if existing else None,
    )


def _registered_marketplace_source(cowork_root: Path) -> Path | None:
    path = cowork_root / "cowork_plugins/known_marketplaces.json"
    entry = config_io.read_json_safe(path).get(MARKETPLACE_NAME)
    if not isinstance(entry, dict):
        return None
    source = entry.get("source")
    if not isinstance(source, dict):
        return None
    raw_path = source.get("path")
    if source.get("source") == "directory" and (not isinstance(raw_path, str) or not raw_path):
        return None
    if (
        source.get("source") != "directory"
        or not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
    ):
        raise RuntimeError("Existing UG marketplace is not a local directory; left unchanged.")
    return Path(raw_path)


def _install_plugin(
    config_root: Path,
    marketplace_root: Path,
    *,
    cowork: bool = True,
    marketplace_name: str = MARKETPLACE_NAME,
    plugin_id: str = PLUGIN_ID,
) -> None:
    claude = _claude_binary()
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_root)
    _run_claude(
        claude,
        ["plugin", "validate", str(marketplace_root)],
        env,
    )
    previous_source = _registered_marketplace_source(config_root) if cowork else None
    switching_source = (
        previous_source is not None and previous_source.resolve() != marketplace_root.resolve()
    )
    previous_plugin_id = plugin_id
    if switching_source:
        installed = _run_claude_json(claude, ["plugin", "list", "--cowork", "--json"], env)
        if _contains_plugin_id(installed, plugin_id=LEGACY_PLUGIN_ID) and not _contains_plugin_id(
            installed, plugin_id=plugin_id
        ):
            previous_plugin_id = LEGACY_PLUGIN_ID
    try:
        if switching_source:
            _remove_plugin_registration(config_root)
        _register_plugin(claude, env, marketplace_root, cowork, marketplace_name, plugin_id)
    except Exception as exc:
        if switching_source and previous_source is not None:
            try:
                _remove_plugin_registration(config_root)
                _register_plugin(
                    claude, env, previous_source, cowork, marketplace_name, previous_plugin_id
                )
            except Exception as recovery_error:
                raise RuntimeError(
                    f"{exc} Could not restore the previous UG plugin: {recovery_error}. "
                    "Run `ug configure` again to repair the installation."
                ) from exc
        raise


def _register_plugin(
    claude: str,
    env: dict[str, str],
    marketplace_root: Path,
    cowork: bool,
    marketplace_name: str,
    plugin_id: str,
) -> None:
    scope = ["--cowork"] if cowork else []
    _run_claude_json(
        claude,
        ["plugin", "marketplace", "add", str(marketplace_root), *scope, "--json"],
        env,
        accepted_failures=("already exists", "already_exists", "already registered"),
    )
    _run_claude_json(
        claude,
        [
            "plugin",
            "marketplace",
            "update",
            marketplace_name,
            *scope,
            "--json",
        ],
        env,
    )
    installed = _run_claude_json(
        claude,
        ["plugin", "list", *scope, "--json"],
        env,
    )
    action = "update" if _contains_plugin_id(installed, plugin_id=plugin_id) else "install"
    _run_claude_json(
        claude,
        ["plugin", action, plugin_id, *scope, "--json"],
        env,
    )
    if (
        cowork
        and plugin_id != LEGACY_PLUGIN_ID
        and _contains_plugin_id(installed, plugin_id=LEGACY_PLUGIN_ID)
    ):
        try:
            _run_claude_json(
                claude,
                ["plugin", "uninstall", LEGACY_PLUGIN_ID, "--cowork", "--json"],
                env,
                accepted_failures=("not installed", "not_installed", "not found"),
            )
        except Exception as exc:
            print_warning(
                f"Could not remove legacy Claude Desktop plugin {LEGACY_PLUGIN_ID}: {exc}. "
                "The updated plugin and native connectors remain configured. "
                "Run `ug configure` again to retry cleanup."
            )


def _claude_binary() -> str:
    executable = shutil.which("claude")
    if not executable or not Path(executable).is_absolute():
        raise RuntimeError("Claude CLI is required to configure Unity Gateway plugins.")
    return executable


def _run_claude(claude: str, args: list[str], env: dict[str, str]) -> str:
    result = subprocess_cross_os.run(
        [claude, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"`claude {' '.join(args[:3])}` failed{f': {detail}' if detail else ''}."
        )
    return result.stdout or ""


def _run_claude_json(
    claude: str,
    args: list[str],
    env: dict[str, str],
    *,
    accepted_failures: tuple[str, ...] = (),
) -> object:
    result = subprocess_cross_os.run(
        [claude, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    lowered = output.lower()
    if result.returncode != 0:
        if any(marker in lowered for marker in accepted_failures):
            return {}
        raise RuntimeError(
            f"`claude {' '.join(args[:3])}` failed{f': {output}' if output else ''}."
        )
    try:
        payload = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"`claude {' '.join(args[:3])}` returned invalid JSON.") from exc
    if isinstance(payload, dict) and payload.get("outcome") not in (None, "ok", "success"):
        message = str(payload.get("message") or "Claude plugin command failed.")
        if any(marker in message.lower() for marker in accepted_failures):
            return {}
        raise RuntimeError(message)
    return payload


def _contains_plugin_id(payload: object, *, plugin_id: str = PLUGIN_ID) -> bool:
    if isinstance(payload, str):
        return payload == plugin_id
    if isinstance(payload, list):
        return any(_contains_plugin_id(value, plugin_id=plugin_id) for value in payload)
    if isinstance(payload, dict):
        installed_id = payload.get("pluginId") or payload.get("id")
        if installed_id == plugin_id:
            return True
        name, _, marketplace = plugin_id.partition("@")
        if payload.get("name") == name and payload.get("marketplace") == marketplace:
            return True
        return any(_contains_plugin_id(value, plugin_id=plugin_id) for value in payload.values())
    return False


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(payload))


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _marketplace_root() -> Path:
    return config_io.APP_DIR / MARKETPLACE_DIRECTORY_NAME


def _state_path() -> Path:
    return config_io.APP_DIR / STATE_FILENAME
