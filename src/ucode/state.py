"""Persistent state for ucode (per-workspace, versioned)."""

from __future__ import annotations

import json
from typing import cast

from ucode.config_io import APP_DIR, is_dry_run
from ucode.custom_oauth import (
    CUSTOM_OAUTH_TIMEOUT_MS,
    CustomOAuthConfig,
    build_custom_auth_shell_command,
    build_custom_auth_token_argv,
)
from ucode.databricks import (
    build_auth_shell_command,
    build_auth_token_argv,
    build_shared_base_urls,
)
from ucode.managed_files import CONFIG_SCOPES, DEFAULT_CONFIG_SCOPE
from ucode.workspace import canonical_workspace_key, split_workspace_key, workspace_key

STATE_PATH = APP_DIR / "state.json"
STATE_VERSION = 3
CONFIG_SCOPE_KEY = "config_scope"
# Transient key holding the developer's own values for whatever a managed config layered over them.
# Present only in memory: the layered values render the agent settings files, while `save_state`
# restores what's under it so `state.json` keeps recording the developer's own configuration.
MANAGED_OVERLAY_KEY = "_managed_overlay"
# On a host that serves several workspaces, ``current_workspace`` holds the bare host (what older
# ug versions and integrations read) and ``current_workspace_key`` the ``<host>?w=<id>`` block.
CURRENT_WORKSPACE_KEY = "current_workspace_key"
# The copy of a multi-workspace host's current block kept under the bare host for integrations that
# look ucode state up by the host alone. ug itself never reads it.
ALIAS_OF_KEY = "alias_of"
AUTH_COMMAND_TIMEOUT_MS = 5000
AUTH_REFRESH_INTERVAL_MS = 900_000


def load_full_state() -> dict:
    """Load the entire state file. Returns empty structure if missing or wrong version."""
    if not STATE_PATH.exists():
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    if not isinstance(data, dict) or data.get("state_version") != STATE_VERSION:
        return {"state_version": STATE_VERSION, "current_workspace": None, "workspaces": {}}
    _migrate_legacy_workspace_keys(data)
    return data


def _canonical_saved_key(key: str, block: object) -> str:
    """Like :func:`canonical_workspace_key`, also for a ``<host>?w=<id>`` key an older version saved.

    This version writes ``workspace_id`` into every block it keys that way.
    """
    host, workspace_id = split_workspace_key(key)
    if workspace_id and not (isinstance(block, dict) and block.get("workspace_id") == workspace_id):
        return host
    return canonical_workspace_key(key)


def _migrate_legacy_workspace_keys(data: dict) -> None:
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        return
    for key in list(workspaces):
        canonical = _canonical_saved_key(key, workspaces[key])
        if canonical == key or canonical in workspaces:
            continue
        block = workspaces.pop(key)
        if isinstance(block, dict) and "workspace" in block:
            block["workspace"] = canonical
        workspaces[canonical] = block
        if data.get("current_workspace") == key:
            data["current_workspace"] = canonical


def current_workspace_key(full: dict) -> str | None:
    """The ``workspaces`` key of the current workspace.

    ``current_workspace_key`` counts only while it matches ``current_workspace``: an older ug that
    switched workspaces updates the bare host alone.
    """
    current = full.get("current_workspace")
    if not isinstance(current, str) or not current:
        return None
    key = full.get(CURRENT_WORKSPACE_KEY)
    if isinstance(key, str) and split_workspace_key(key)[0] == current:
        return key
    return current


def load_config_scope() -> str:
    scope = load_full_state().get(CONFIG_SCOPE_KEY)
    return scope if scope in CONFIG_SCOPES else DEFAULT_CONFIG_SCOPE


def save_config_scope(scope: str) -> None:
    if is_dry_run() or load_config_scope() == scope:
        return
    full = load_full_state()
    full[CONFIG_SCOPE_KEY] = scope
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write state file: {STATE_PATH}") from exc


def load_state() -> dict:
    """Load the current workspace's state as a flat dict."""
    full = load_full_state()
    key = current_workspace_key(full)
    if not key:
        return {}
    ws_state = full.get("workspaces", {}).get(key, {})
    if not isinstance(ws_state, dict):
        ws_state = {}
    workspace, workspace_id = split_workspace_key(key)
    ws_state["workspace"] = workspace
    if workspace_id:
        ws_state["workspace_id"] = workspace_id
    else:
        ws_state.pop("workspace_id", None)
    ws_state.pop(ALIAS_OF_KEY, None)
    return hydrate_state(ws_state)


def save_state(state: dict) -> None:
    """Save workspace state back into the per-workspace structure.

    Values a managed config layered over the developer's own are stripped first (see
    ``MANAGED_OVERLAY_KEY``), so an admin-published config takes effect through the generated agent
    settings files without overwriting what the developer configured for themselves. Read
    non-destructively: a launch can save more than once from the same dict (e.g. the relayed proxy
    rewriting its port), and every one of those writes must restore the developer's values.
    """
    if is_dry_run():
        return
    full = load_full_state()
    workspace = state.get("workspace")
    if workspace:
        workspace_id = state.get("workspace_id")
        key = workspace_key(workspace, workspace_id if isinstance(workspace_id, str) else None)
    else:
        key = current_workspace_key(full)
    if key:
        block = hydrate_state(_without_managed_overlay(state))
        _write_workspace_block(full, key, block)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write state file: {STATE_PATH}") from exc


def _write_workspace_block(full: dict, key: str, block: dict) -> None:
    """Store ``block`` under ``key`` and select it, keeping the bare-host alias in step."""
    workspaces = full.setdefault("workspaces", {})
    host, workspace_id = split_workspace_key(key)
    block.pop(ALIAS_OF_KEY, None)
    if workspace_id:
        block["workspace_id"] = workspace_id
        workspaces[key] = block
        workspaces[host] = {**block, ALIAS_OF_KEY: key}
        full[CURRENT_WORKSPACE_KEY] = key
    else:
        block.pop("workspace_id", None)
        workspaces[key] = block
        full.pop(CURRENT_WORKSPACE_KEY, None)
    full["current_workspace"] = host


def _without_managed_overlay(state: dict) -> dict:
    """Return ``state`` with managed-config values swapped back for the developer's own.

    Returns a new dict and leaves ``state`` untouched, so the caller keeps the layered values it
    needs for rendering and repeated saves stay idempotent.
    """
    overlay = state.get(MANAGED_OVERLAY_KEY)
    if not isinstance(overlay, dict):
        return state
    persisted = {key: value for key, value in state.items() if key != MANAGED_OVERLAY_KEY}
    for key, value in overlay.items():
        # A key the developer never set is dropped rather than persisted as None.
        if value is None:
            persisted.pop(key, None)
        else:
            persisted[key] = value
    return persisted


def set_current_workspace(workspace: str | None) -> None:
    """Select ``workspace`` (a ``workspaces`` key) without touching the per-workspace blocks.

    Used by flows that operate on a non-current workspace and must not silently
    change which workspace ``ucode launch`` targets afterwards."""
    if is_dry_run():
        return
    full = load_full_state()
    if current_workspace_key(full) == workspace:
        return
    if workspace is None:
        full["current_workspace"] = None
        full.pop(CURRENT_WORKSPACE_KEY, None)
    else:
        host, workspace_id = split_workspace_key(workspace)
        alias_block = full.get("workspaces", {}).get(host)
        if not workspace_id and isinstance(alias_block, dict):
            # The bare host of a multi-workspace host means its current workspace.
            aliased = alias_block.get(ALIAS_OF_KEY)
            if isinstance(aliased, str):
                workspace = aliased
                host, workspace_id = split_workspace_key(aliased)
        full["current_workspace"] = host
        if workspace_id:
            full[CURRENT_WORKSPACE_KEY] = workspace
            block = full.get("workspaces", {}).get(workspace)
            if isinstance(block, dict):
                full["workspaces"][host] = {**block, ALIAS_OF_KEY: workspace}
        else:
            full.pop(CURRENT_WORKSPACE_KEY, None)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write state file: {STATE_PATH}") from exc


def hydrate_state(state: dict) -> dict:
    """Normalize a workspace state entry and add derived harness config.

    :param state: Raw workspace state entry from ``state.json``.
    :returns: Hydrated workspace state with stable ``managed_configs``,
        ``base_urls``, and per-agent ``agents`` entries.
    """
    if not isinstance(state, dict):
        return {}

    hydrated = dict(state)
    managed_configs = hydrated.get("managed_configs")
    if not isinstance(managed_configs, dict):
        managed_configs = {}
    normalized: dict[str, dict] = {}
    for tool, entry in managed_configs.items():
        if isinstance(entry, dict):
            keys = entry.get("keys") if isinstance(entry.get("keys"), list) else []
            normalized[tool] = {"keys": keys}
        elif entry:
            normalized[tool] = {"keys": []}
    hydrated["managed_configs"] = normalized

    workspace = hydrated.get("workspace")
    if workspace:
        hydrated["base_urls"] = build_shared_base_urls(workspace)
        hydrated["agents"] = build_agent_state(hydrated)
    else:
        hydrated["base_urls"] = {}
        hydrated["agents"] = {}

    return hydrated


def build_agent_state(state: dict) -> dict[str, dict]:
    """Build per-agent harness configuration for a workspace.

    The returned shape is intended for downstream tools that want to reuse
    ucode's configured gateway URLs and auth command without duplicating
    endpoint construction logic.

    :param state: Hydrated workspace state containing ``workspace``,
        ``base_urls``, and discovered model lists.
    :returns: Mapping from agent name to its reusable configuration.
    """
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return {}

    from ucode.agents import default_model_for_tool

    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    base_urls_value = state.get("base_urls")
    base_urls = base_urls_value if isinstance(base_urls_value, dict) else {}
    use_pat = bool(state.get("use_pat"))
    auth_command = build_auth_shell_command(workspace, profile, use_pat=use_pat)
    auth_argv = build_auth_token_argv(workspace, profile, use_pat=use_pat)
    claude_auth_command = auth_command
    codex_auth_command = auth_command
    codex_auth_argv = auth_argv
    codex_auth_timeout_ms = AUTH_COMMAND_TIMEOUT_MS
    custom_oauth = state.get("custom_oauth")
    if isinstance(custom_oauth, dict):
        typed_custom_oauth = cast(CustomOAuthConfig, custom_oauth)
        claude_auth_command = build_custom_auth_shell_command(workspace, typed_custom_oauth)
        codex_auth_command = claude_auth_command
        codex_auth_argv = build_custom_auth_token_argv(workspace, typed_custom_oauth)
        codex_auth_timeout_ms = CUSTOM_OAUTH_TIMEOUT_MS
    claude_models_value = state.get("claude_models")
    claude_models: dict = claude_models_value if isinstance(claude_models_value, dict) else {}
    codex_models_value = state.get("codex_models")
    codex_models = codex_models_value if isinstance(codex_models_value, list) else []
    gemini_models_value = state.get("gemini_models")
    gemini_models = gemini_models_value if isinstance(gemini_models_value, list) else []

    selection_state = {
        **state,
        "claude_models": claude_models,
        "codex_models": codex_models,
        "gemini_models": gemini_models,
    }

    claude_model = (
        claude_models.get("opus") or claude_models.get("sonnet") or claude_models.get("haiku")
    )
    codex_model = default_model_for_tool("codex", selection_state)
    pi_model = default_model_for_tool("pi", selection_state)

    agents: dict[str, dict] = {
        "claude": {
            "model": claude_model,
            "base_url": base_urls.get("claude"),
            "auth_command": claude_auth_command,
            "auth_refresh_interval_ms": AUTH_REFRESH_INTERVAL_MS,
            "env": {
                "ANTHROPIC_BASE_URL": base_urls.get("claude"),
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(AUTH_REFRESH_INTERVAL_MS),
                # Kept in sync with agents/claude.py render_overlay.
                "ENABLE_PROMPT_CACHING_1H": "1",
                "ENABLE_TOOL_SEARCH": "true",
                "CLAUDE_CODE_USE_GATEWAY": "1",
            },
        },
        "codex": {
            "model": codex_model,
            "base_url": base_urls.get("codex"),
            "auth_command": codex_auth_command,
            "auth": {
                "command": codex_auth_argv[0],
                "args": codex_auth_argv[1:],
                "timeout_ms": codex_auth_timeout_ms,
                "refresh_interval_ms": AUTH_REFRESH_INTERVAL_MS,
            },
        },
        "pi": {
            "model": pi_model,
            "base_urls": base_urls.get("pi") if isinstance(base_urls.get("pi"), dict) else {},
            "auth_command": auth_command,
            "auth_refresh_interval_ms": AUTH_REFRESH_INTERVAL_MS,
        },
    }
    return {
        name: {key: value for key, value in config.items() if value is not None}
        for name, config in agents.items()
    }


def clear_state() -> None:
    """Remove the current workspace entry and the config scope from state."""
    full = load_full_state()
    full.pop(CONFIG_SCOPE_KEY, None)
    key = current_workspace_key(full)
    if key:
        workspaces = full.get("workspaces", {})
        workspaces.pop(key, None)
        host, workspace_id = split_workspace_key(key)
        if workspace_id and isinstance(workspaces.get(host), dict):
            if workspaces[host].get(ALIAS_OF_KEY) == key:
                workspaces.pop(host, None)
        full["current_workspace"] = None
        full.pop(CURRENT_WORKSPACE_KEY, None)
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(full, indent=2), encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to clear state file: {STATE_PATH}") from exc


SELF_MANAGED_AGENTS_KEY = "self_managed_agents"


def self_managed_agents(state: dict) -> list[str]:
    """The agents the developer has opted into self-managing for this workspace."""
    value = state.get(SELF_MANAGED_AGENTS_KEY)
    return list(value) if isinstance(value, list) else []


def is_self_managed(state: dict, tool: str) -> bool:
    """True when the developer has explicitly added ``tool`` to their self-managed list."""
    return tool in self_managed_agents(state)


def workspace_self_managed_agents(workspace: str) -> list[str]:
    """The self-managed list persisted for ``workspace`` specifically (empty if none).

    The opt-in is per workspace, so a configure that switches workspaces must read the
    destination's own list rather than carry the source workspace's forward.
    """
    workspaces = load_full_state().get("workspaces", {})
    block = workspaces.get(workspace)
    host, workspace_id = split_workspace_key(workspace)
    if block is None and workspace_id:
        # A workspace first configured with its id inherits a bare-host block saved before.
        block = workspaces.get(host)
    if not isinstance(block, dict) or ALIAS_OF_KEY in block:
        return []
    value = block.get(SELF_MANAGED_AGENTS_KEY)
    return list(value) if isinstance(value, list) else []


def add_self_managed_agent(state: dict, tool: str) -> dict:
    """Record ``tool`` as self-managed; no-op if already present."""
    agents = self_managed_agents(state)
    if tool not in agents:
        agents.append(tool)
        state[SELF_MANAGED_AGENTS_KEY] = agents
    return state


def remove_self_managed_agent(state: dict, tool: str) -> dict:
    """Remove ``tool`` from the self-managed list; no-op if not present."""
    agents = self_managed_agents(state)
    if tool in agents:
        agents.remove(tool)
        if agents:
            state[SELF_MANAGED_AGENTS_KEY] = agents
        else:
            state.pop(SELF_MANAGED_AGENTS_KEY, None)
    return state


def is_tool_managed(state: dict, tool: str) -> bool:
    """True once configure has written ucode's config for ``tool``.

    Used to distinguish a config file the user owned before ucode ran from one
    ucode itself generated: only the former is backup-worthy, because revert
    restores a backup in place instead of deleting ucode's generated file.
    """
    return bool((state.get("managed_configs") or {}).get(tool))


def mark_tool_managed(state: dict, tool: str, managed_keys: list) -> dict:
    """Record which config keys ucode manages for ``tool``."""
    managed_configs = dict(state.get("managed_configs") or {})
    managed_configs[tool] = {"keys": list(managed_keys)}
    state["managed_configs"] = managed_configs
    state["last_tool"] = tool
    return state


def get_provider_service(state: dict, tool: str) -> str | None:
    """Return the persisted Model Provider Service for ``tool``, if any.

    Launches route through this provider (skipping Databricks model pinning)
    unless overridden by an explicit ``--provider`` flag.
    """
    providers = state.get("provider_services")
    if not isinstance(providers, dict):
        return None
    name = providers.get(tool)
    return name if isinstance(name, str) and name else None


def set_provider_service(state: dict, tool: str, full_name: str | None) -> dict:
    """Persist (or clear, when ``full_name`` is None) ``tool``'s provider service."""
    providers = dict(state.get("provider_services") or {})
    if full_name:
        providers[tool] = full_name
    else:
        providers.pop(tool, None)
    if providers:
        state["provider_services"] = providers
    else:
        state.pop("provider_services", None)
    return state


def get_model_location(state: dict, tool: str) -> str | None:
    """Return the persisted Unity Catalog model location (``<catalog>.<schema>``) for ``tool``.

    Launches discover models in this schema instead of ``system.ai`` unless overridden by an
    explicit ``--model-location`` or ``--provider`` flag.
    """
    locations = state.get("model_locations")
    if not isinstance(locations, dict):
        return None
    location = locations.get(tool)
    return location if isinstance(location, str) and location else None


def set_model_location(state: dict, tool: str, location: str | None) -> dict:
    """Persist (or clear, when ``location`` is None) ``tool``'s model location."""
    locations = dict(state.get("model_locations") or {})
    if location:
        locations[tool] = location
    else:
        locations.pop(tool, None)
    if locations:
        state["model_locations"] = locations
    else:
        state.pop("model_locations", None)
    return state


# The managed configuration's ``update_time`` last applied to this workspace's agents. A launch
# compares a freshly fetched config against it to decide whether to re-apply, so it is written only
# after an apply succeeds — never on a plain fetch.
APPLIED_MANAGED_UPDATE_TIME_KEY = "applied_managed_update_time"


def get_applied_managed_update_time(state: dict) -> str | None:
    """The ``update_time`` of the managed configuration last applied to this workspace, if any."""
    value = state.get(APPLIED_MANAGED_UPDATE_TIME_KEY)
    return value if isinstance(value, str) and value else None


def set_applied_managed_update_time(state: dict, update_time: str | None) -> dict:
    """Record (or clear) the applied managed configuration watermark for this workspace."""
    if update_time:
        state[APPLIED_MANAGED_UPDATE_TIME_KEY] = update_time
    else:
        state.pop(APPLIED_MANAGED_UPDATE_TIME_KEY, None)
    return state
