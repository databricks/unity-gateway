"""Install Unity Gateway workspace skills and MCP servers into Claude Cowork."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

from ucode import config_io
from ucode.agents.desktop.claude import claude_desktop_root_directory
from ucode.databricks import build_mcp_proxy_argv
from ucode.mcp_connection_login import connection_from_url
from ucode.os_compatibility import subprocess_cross_os
from ucode.skills_state import records_for_scope
from ucode.ui import print_warning

MARKETPLACE_NAME = "unity-gateway-cowork"
PLUGIN_NAME = "ug"
PLUGIN_DISPLAY_NAME = "ug"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
LEGACY_PLUGIN_ID = f"unity-gateway@{MARKETPLACE_NAME}"
STATE_FILENAME = "claude-cowork.json"
MARKETPLACE_DIRECTORY_NAME = "claude-cowork-marketplace"
CONNECTOR_SKILL_NAME = "unity-gateway-connectors"
SESSION_START_HOOK_TIMEOUT_SECONDS = 30

_COWORK_MARKERS = (
    "cowork_settings.json",
    "cowork_plugins",
    "cowork_plugins/known_marketplaces.json",
    "cowork_plugins/installed_plugins.json",
)


def configure_cowork(state: dict, mcp_servers: list[dict]) -> bool:
    """Generate and install UG's Cowork plugin for the active workspace."""

    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        raise RuntimeError("No Databricks workspace is configured for Cowork.")

    previous_state = config_io.read_json_safe(_state_path())
    skill_records = _workspace_skill_records(workspace)
    if not previous_state and not skill_records and not mcp_servers:
        return False
    if not previous_state and not _cowork_sessions_root_exists():
        return False

    profile = state.get("profile")
    if not isinstance(profile, str) or not profile:
        raise RuntimeError("Select an explicit Databricks CLI profile for Cowork.")

    mcp_config = _cowork_mcp_config(
        mcp_servers,
        workspace,
        profile,
        use_pat=bool(state.get("use_pat")),
        custom_oauth=bool(state.get("custom_oauth")),
    )
    if not previous_state and not skill_records and not mcp_config["mcpServers"]:
        return False

    cowork_root = discover_cowork_root()
    previous_root = previous_state.get("cowork_root")
    if isinstance(previous_root, str) and previous_root and Path(previous_root) != cowork_root:
        _remove_plugin_registration(Path(previous_root))
    marketplace_root = _marketplace_root()
    plugin_root = marketplace_root / "plugins" / PLUGIN_NAME
    hooks = _session_start_hooks(mcp_config, workspace, profile, use_pat=bool(state.get("use_pat")))
    _write_marketplace(marketplace_root, plugin_root, skill_records, mcp_config, hooks)
    config_io.atomic_write_json(
        _state_path(),
        {"version": 1, "cowork_root": str(cowork_root)},
    )
    _install_plugin(cowork_root, marketplace_root)
    return True


def revert_cowork() -> bool:
    """Remove only UG's Cowork plugin, marketplace registration, and generated source."""

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


def _remove_plugin_registration(cowork_root: Path) -> bool:
    """Remove UG's plugin and marketplace registration from one Cowork root."""

    claude = _claude_binary()
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(cowork_root)
    plugin_result = False
    for plugin_id in (PLUGIN_ID, LEGACY_PLUGIN_ID):
        result = _run_claude_json(
            claude,
            ["plugin", "uninstall", plugin_id, "--cowork", "--json"],
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
            MARKETPLACE_NAME,
            "--cowork",
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
        executable = Path(argv[0])
        if not executable.is_absolute():
            raise RuntimeError(
                "Could not resolve the absolute `ug` executable required by Claude Cowork."
            )
        entries[name] = {"type": "stdio", "command": str(executable), "args": argv[1:]}
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


def _write_marketplace(
    marketplace_root: Path,
    plugin_root: Path,
    skill_records: list[dict],
    mcp_config: dict,
    hooks: dict | None = None,
) -> None:
    marketplace_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{marketplace_root.name}-", dir=marketplace_root.parent)
    )
    backup = marketplace_root.with_name(f".{marketplace_root.name}.previous")
    try:
        staging_plugin = staging / plugin_root.relative_to(marketplace_root)
        (staging / ".claude-plugin").mkdir(parents=True)
        (staging_plugin / ".claude-plugin").mkdir(parents=True)
        marketplace: dict[str, object] = {
            "name": MARKETPLACE_NAME,
            "owner": {"name": "Databricks"},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "displayName": PLUGIN_DISPLAY_NAME,
                    "description": "Unity Gateway workspace skills and MCP servers for Cowork",
                    "source": f"./plugins/{PLUGIN_NAME}",
                }
            ],
        }
        plugin: dict[str, object] = {
            "name": PLUGIN_NAME,
            "displayName": PLUGIN_DISPLAY_NAME,
            "description": "Unity Gateway workspace skills and MCP servers for Cowork",
            "author": {"name": "Databricks"},
        }
        if mcp_config["mcpServers"]:
            plugin["mcpServers"] = "./.mcp.json"
            _write_json(staging_plugin / ".mcp.json", mcp_config)
        if hooks:
            _write_json(staging_plugin / "hooks" / "hooks.json", hooks)
        _write_json(staging / ".claude-plugin" / "marketplace.json", marketplace)
        _write_json(staging_plugin / ".claude-plugin" / "plugin.json", plugin)
        _copy_skills(staging_plugin / "skills", skill_records)
        if mcp_config["mcpServers"]:
            _write_connector_skill(staging_plugin / "skills", list(mcp_config["mcpServers"]))

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


def _session_start_hooks(
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
    argv = [command, "cowork-session-start", "--host", workspace.rstrip("/")]
    argv += ["--profile", profile]
    if use_pat:
        argv.append("--use-pat")
    for name, url in urls.items():
        argv += ["--server", f"{name}={url}"]
    hook = {
        "type": "command",
        "command": subprocess_cross_os.shell_command(argv),
        "timeout": SESSION_START_HOOK_TIMEOUT_SECONDS,
    }
    return {"hooks": {"SessionStart": [{"hooks": [hook]}]}}


def _write_connector_skill(destination: Path, server_names: list[str]) -> None:
    # Skill descriptions are in context from the first turn; plugin MCP tools connect
    # lazily, so without this Claude asks whether a connector exists instead of using it.
    skill_dir = destination / CONNECTOR_SKILL_NAME
    if skill_dir.exists():
        return
    connectors = ", ".join(f"`{name}`" for name in server_names)
    description = (
        f"Unity Gateway connectors are available in this session: {connectors}. "
        "Use this skill whenever a request needs one of them, such as the user's own pull "
        "requests, issues, tickets, pages, mail, or files, and always before saying a "
        "connector is unavailable or asking whether the service is connected."
    )
    guidance = (
        f"Unity Gateway connectors configured for this session: {connectors}.\n\n"
        "These connectors start in the background when the session opens, so their tools "
        "can be missing for the first few seconds. A missing tool is not evidence that the "
        "connector is disconnected.\n\n"
        "1. Search for the connector's tools by its name and call the one that fits.\n"
        "2. If no matching tool is found, or the call fails because the tool or server is "
        "not available, wait 5 seconds (for example, run `sleep 5` in the shell), then "
        "search and try again.\n"
        "3. If it is still not available, wait another 5 seconds and try once more.\n"
        "4. If it is still not available after that, tell the user which connector did not "
        "load and quote any error you saw. Do not guess at the cause.\n\n"
        "Only these not-yet-loaded cases need the wait. If the tool returns a real result "
        "or error, such as a permission denial or a sign-in request, handle that instead."
    )
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {CONNECTOR_SKILL_NAME}\ndescription: {json.dumps(description)}\n---\n\n"
        f"{guidance}\n",
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


def _install_plugin(cowork_root: Path, marketplace_root: Path) -> None:
    claude = _claude_binary()
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(cowork_root)
    _run_claude(
        claude,
        ["plugin", "validate", str(marketplace_root)],
        env,
    )
    _run_claude_json(
        claude,
        ["plugin", "marketplace", "add", str(marketplace_root), "--cowork", "--json"],
        env,
        accepted_failures=("already exists", "already_exists", "already registered"),
    )
    _run_claude_json(
        claude,
        [
            "plugin",
            "marketplace",
            "update",
            MARKETPLACE_NAME,
            "--cowork",
            "--json",
        ],
        env,
    )
    installed = _run_claude_json(
        claude,
        ["plugin", "list", "--cowork", "--json"],
        env,
    )
    action = "update" if _contains_plugin_id(installed) else "install"
    _run_claude_json(
        claude,
        ["plugin", action, PLUGIN_ID, "--cowork", "--json"],
        env,
    )
    if _contains_plugin_id(installed, plugin_id=LEGACY_PLUGIN_ID):
        _run_claude_json(
            claude,
            ["plugin", "uninstall", LEGACY_PLUGIN_ID, "--cowork", "--json"],
            env,
            accepted_failures=("not installed", "not_installed", "not found"),
        )


def _claude_binary() -> str:
    executable = shutil.which("claude")
    if not executable or not Path(executable).is_absolute():
        raise RuntimeError("Claude CLI is required to configure Cowork plugins.")
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
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _marketplace_root() -> Path:
    return config_io.APP_DIR / MARKETPLACE_DIRECTORY_NAME


def _state_path() -> Path:
    return config_io.APP_DIR / STATE_FILENAME
