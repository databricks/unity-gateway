"""Claude Code agent: writes ~/.claude/settings.json env block."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from ucode import gateway_proxy
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    prune_key_paths,
    read_json_safe,
    write_json_file,
)
from ucode.constants import (
    AGENT_CLAUDE,
    LOOPBACK_HOST,
    MCP_CLEANUP_SCOPES,
    MCP_USER_SCOPE,
    MODEL_PROVIDER_SERVICE_HEADER,
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.custom_oauth import (
    CustomOAuthConfig,
    build_custom_auth_shell_command,
    custom_oauth_cli_enabled,
)
from ucode.databricks import (
    AnthropicModelCatalog,
    _debug,
    build_auth_shell_command,
    build_otel_headers_shell_command,
    build_otel_traces_endpoint,
    build_tool_base_url,
    get_databricks_token,
    ug_binary,
)
from ucode.launcher import exec_or_spawn
from ucode.managed_config import refresh_managed_config
from ucode.managed_files import (
    OS,
    ManagedFileSnapshots,
    ManagedFileWriteUnavailable,
    SettingsPassthrough,
    apply_settings_passthrough,
    current_os,
    managed_file_conflicts,
    managed_file_is_verified,
    managed_file_scope,
    managed_file_snapshots,
    managed_file_status,
    managed_files_supported,
    managed_writes_allowed,
    mark_managed_file_verified,
    plan_settings_passthrough,
    read_managed_file,
    reconcile_managed_file,
    record_settings_passthrough,
    record_ug_picker,
    revert_managed_file,
    warn_skipped_settings_passthrough,
    withdrawn_list_items,
)
from ucode.mcp_oauth import (
    CLAUDE_CODE_OAUTH_CLIENT_ID,
    MCP_OAUTH_CALLBACK_PORT,
    oauth_client_available,
)
from ucode.mcp_web_search import (
    AUTOMATIC_PROVIDER,
    EXTERNAL_PROVIDER_OVERRIDE_FLAG,
    MANAGED_ENTRY_FLAG,
    PROVIDER_ENV,
    external_provider_selected,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing import v2 as smart_routing_v2
from ucode.smart_routing.claude_hooks import (
    FIRST_PROMPT_SOCKET_ENV,
    remove_smart_routing_hooks,
    sync_smart_routing_hooks,
)
from ucode.smart_routing.routing import configured_router_name
from ucode.state import MANAGED_OVERLAY_KEY, is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version
from ucode.ui import print_note, print_success, print_warning

from .args import LaunchOptions, has_explicit_model_arg

GATEWAY_MODEL_DISCOVERY_ENV_VAR = "ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY"
# If set, Claude Code launches in headless mode instead of the interactive login flow.
CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFIG_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "ucode-settings.json"
CLAUDE_MCP_CONFIG_PATH = Path.home() / ".claude.json"
# The default model is stored in Claude's default user settings, not the ucode settings.
CLAUDE_USER_SETTINGS_PATH = CLAUDE_CONFIG_DIR / "settings.json"
CLAUDE_BACKUP_PATH = APP_DIR / "claude-ucode-settings.backup.json"
WEB_SEARCH_MCP_STATE_KEY = "claude_web_search_mcp"
MINIMUM_CLAUDE_VERSION = (2, 1, 290)
MINIMUM_CLAUDE_VERSION_TEXT = "2.1.290"
MANAGED_MCP_SETTINGS_KEY = "managedMcpServers"

SPEC: ToolSpec = {
    "binary": "claude",
    "package": "@anthropic-ai/claude-code",
    "display": "Claude Code",
    "config_path": CLAUDE_SETTINGS_PATH,
    "backup_path": CLAUDE_BACKUP_PATH,
}

# Retained only to identify and remove state written by the legacy persisted opt-in.
SMART_ROUTING_STATE_KEY = smart_routing_v2.LEGACY_STATE_KEY


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def minimum_version_error() -> str | None:
    version = agent_version(SPEC["binary"])
    parsed = _parse_version(version)
    if parsed is None or parsed >= MINIMUM_CLAUDE_VERSION:
        return None
    return (
        f"ug requires Claude Code {MINIMUM_CLAUDE_VERSION_TEXT} or newer. "
        f"Your current version is Claude Code {version}."
    )


def _resolve_web_search_model(state: dict) -> str | None:
    """Pick the model the web_search MCP server should call. Prefers an
    explicit override in state, otherwise the first endpoint discovered as
    Responses-API-capable. Returns None if no GPT endpoint is available —
    callers should skip the MCP wiring in that case."""
    override = state.get("web_search_model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    codex_models = state.get("codex_models") or []
    if isinstance(codex_models, list) and codex_models:
        first = codex_models[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


WEB_SEARCH_MCP_NAME = "web_search"
# Matches both the AI Gateway form (`databricks-claude-opus-4-8`) and the UC
# model-services form (`system.ai.claude-opus-4-8`).
_CLAUDE_MODEL_RE = re.compile(
    r"^(?:system\.ai\.)?(?:databricks-)?claude-(opus|sonnet)-(\d+)(?:-(\d+))?(.*)$"
)

# OTLP trace-export keys owned by the managed configuration path.
CLAUDE_OTEL_TRACE_ENV_KEYS = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
)
CLAUDE_OTEL_TRACE_PATHS = tuple(("env", key) for key in CLAUDE_OTEL_TRACE_ENV_KEYS) + (
    ("otelHeadersHelper",),
)


def _otel_trace_env(workspace: str) -> dict[str, str]:
    """Build Claude Code's client-side OTLP trace configuration."""
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": build_otel_traces_endpoint(workspace),
        "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS": "900000",
        "CLAUDE_CODE_PROPAGATE_TRACEPARENT": "1",
    }


# Model-selection env keys ucode manages. Existing family defaults in the enterprise-managed file
# are preserved unless Coding Agent Config explicitly supplies that family.
CLAUDE_MANAGED_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
)
CLAUDE_DEFAULT_MODEL_ENV_KEYS = {
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
# Launch-scoped feature flags that ucode may write into Claude settings. These
# must be removed again when the corresponding launch flag is absent.
CLAUDE_CONDITIONAL_ENV_KEYS = ("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",)
# Env keys ucode used to write but no longer does; stripped from the managed
# settings file on every launch so stale values never linger.
CLAUDE_REMOVED_ENV_KEYS = ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",)
CLAUDE_MANAGED_PICKER_KEYS = ("availableModels", "enforceAvailableModels", "modelPicker")
CLAUDE_PERMISSIONS_DENY_PATH = ["permissions", "deny"]
CLAUDE_ALLOWED_MCP_SERVERS_KEY = "allowedMcpServers"
# The managed config's harness-native settings (resolved from the manifest), and the leaves last
# skipped so the warning about them prints once rather than on every launch. What ug delivered is
# recorded in the file-keyed managed manifest instead, because the managed file is machine-wide.
SETTINGS_PASSTHROUGH_STATE_KEY = "claude_settings_passthrough"
SETTINGS_PASSTHROUGH_IGNORED_STATE_KEY = "claude_settings_passthrough_ignored"
# Settings that stop Claude Code from running hooks outside the managed file, which is where smart
# routing installs its per-launch hooks.
CLAUDE_HOOK_LOCKDOWN_KEYS = ("allowManagedHooksOnly", "disableAllHooks")
ANTHROPIC_CUSTOM_HEADERS_ENV_KEY = "ANTHROPIC_CUSTOM_HEADERS"
CLAUDE_MANAGED_CUSTOM_HEADER_NAMES = frozenset(
    {
        "x-databricks-use-coding-agent-mode",
        "user-agent",
        MODEL_PROVIDER_SERVICE_HEADER.casefold(),
        MODEL_SERVICE_PARENT_SCHEMA_HEADER.casefold(),
        SMART_ROUTER_RECIPE_HEADER.casefold(),
    }
)
# These attribute inference traffic; neither selects the gateway or provider.
CLAUDE_OPTIONAL_ATTRIBUTION_HEADER_NAMES = frozenset(
    {"user-agent", SMART_ROUTER_RECIPE_HEADER.casefold()}
)
# Relayed drops the user scope to deliberately omit the stale apiKeyHelper. Only applied to relayed
# launches — normal launches keep loading user settings (hooks/permissions) as before.
_RELAYED_SETTING_SOURCES = "project,local"


def _apply_managed_header_lines(
    ucode_lines: list[str], managed_http_headers: dict[str, str] | None
) -> list[str]:
    """Overlay admin ``managed_http_headers`` onto ucode's header lines; admin wins by name."""
    lines_by_name: dict[str, str] = {}
    for line in ucode_lines:
        name, _separator, _value = line.partition(":")
        lines_by_name[name.strip().casefold()] = line
    for name, value in (managed_http_headers or {}).items():
        lines_by_name[name.strip().casefold()] = f"{name}: {value}"
    return list(lines_by_name.values())


def configured_paths(state: dict) -> list[str]:
    """The Claude config file ug writes; the OS-managed file is added by the dispatcher."""
    return [str(CLAUDE_SETTINGS_PATH)]


def _managed_settings_path() -> Path | None:
    """OS-specific location of Claude Code's enterprise managed-settings.json.
    Returns None on unsupported platforms."""
    if current_os() is OS.LINUX:
        return Path("/etc/claude-code/managed-settings.json")
    if current_os() is OS.MACOS:
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return None


def _parse_managed_settings(text: str) -> dict:
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(settings, dict):
        raise RuntimeError("the top-level JSON value must be an object")
    return settings


def managed_user_agent() -> str | None:
    """The User-Agent value in Claude Code's OS-managed custom headers, if one is there."""
    path = _managed_settings_path()
    try:
        text = read_managed_file(path) if path else None
        settings = _parse_managed_settings(text) if text else {}
    except (RuntimeError, ValueError):  # ValueError: a file that isn't UTF-8
        return None
    env = settings.get("env")
    headers = env.get(ANTHROPIC_CUSTOM_HEADERS_ENV_KEY) if isinstance(env, dict) else None
    if not isinstance(headers, str):
        return None
    values = (_user_agent_header_value(line) for line in headers.split("\n"))
    return next((value for value in values if value is not None), None)


def _user_agent_header_value(line: str) -> str | None:
    """The value of a ``User-Agent`` line in ANTHROPIC_CUSTOM_HEADERS (any name casing), else None."""
    name, separator, value = line.partition(":")
    return value.strip() if separator and name.strip().casefold() == "user-agent" else None


def _dump_managed_settings(settings: dict) -> str:
    return json.dumps(settings, indent=2, sort_keys=True) + "\n"


def managed_settings_are_current(state: dict) -> bool:
    path = _managed_settings_path()
    if path is None:
        return True
    if state.get("claude_relayed"):
        required_scope = "relay-compatible"
    elif managed_writes_allowed():
        required_scope = "managed"
    else:
        required_scope = None
    return managed_file_is_verified(state, "claude", path, required_scope=required_scope)


def gateway_model_discovery_setting_is_absent() -> bool:
    """Return whether model discovery is absent from persistent Claude settings."""
    env = read_json_safe(CLAUDE_SETTINGS_PATH).get("env")
    actual = (
        env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY") if isinstance(env, dict) else None
    )
    return actual is None


def managed_settings_status(state: dict) -> tuple[Path | None, str, str]:
    path = _managed_settings_path()
    status, backup = managed_file_status(state, "claude", path, parser=_parse_managed_settings)
    return path, status, backup


def revert_managed_settings() -> str:
    return revert_managed_file(
        "claude",
        display="Claude Code",
        parser=_parse_managed_settings,
        dumper=_dump_managed_settings,
    )


def _managed_relayed_conflicts(path: Path) -> list[str]:
    """Return managed settings that would override Claude subscription relay auth."""
    text = read_managed_file(path)
    if text is None:
        return []
    try:
        settings = _parse_managed_settings(text)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely inspect Claude Code managed settings at {path}: {exc}. Repair the "
            "file or contact your administrator."
        ) from exc
    conflicts: list[str] = []
    if settings.get("apiKeyHelper"):
        conflicts.append("apiKeyHelper")
    env = settings.get("env")
    if isinstance(env, dict):
        if env.get("ANTHROPIC_BASE_URL"):
            conflicts.append("env.ANTHROPIC_BASE_URL")
        if env.get("ANTHROPIC_CUSTOM_HEADERS"):
            conflicts.append("env.ANTHROPIC_CUSTOM_HEADERS")
    return conflicts


def relayed_proxy_base_url(state: dict) -> str:
    """Loopback base URL for the relayed refresh proxy, allocating a free port
    on first call and caching it in state so config and launch agree."""
    port = state.get("relayed_proxy_port")
    if not isinstance(port, int):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOOPBACK_HOST, 0))
            port = sock.getsockname()[1]
        state["relayed_proxy_port"] = port
    return f"http://{LOOPBACK_HOST}:{port}"


def _web_search_mcp_entry(
    workspace: str,
    search_model: str,
    profile: str | None = None,
) -> dict:
    """Stdio MCP server entry pointing at `ug mcp web-search`. Resolves
    the absolute path to the `ug` binary so launchers without the right
    PATH (e.g. desktop GUI launchers) still find it."""
    env: dict[str, str] = {
        "DATABRICKS_HOST": workspace,
        "UCODE_WEB_SEARCH_MODEL": search_model,
    }
    if profile:
        env["DATABRICKS_CONFIG_PROFILE"] = profile
    return {
        "type": "stdio",
        "command": ug_binary(),
        "args": ["mcp", "web-search", MANAGED_ENTRY_FLAG],
        "env": env,
    }


def render_overlay(
    workspace: str,
    model: str | None,
    claude_models: dict[str, str] | None = None,
    disable_web_search: bool = False,
    profile: str | None = None,
    use_pat: bool = False,
    custom_oauth: CustomOAuthConfig | None = None,
    provider: str | None = None,
    provider_models: dict[str, str] | None = None,
    relayed: bool = False,
    relayed_base_url: str | None = None,
    route_root_model: str | None = None,
    custom_model: str | None = None,
    parent_schema: str | None = None,
    static_models: list[str] | None = None,
    otel_tracing: bool = False,
    picker_catalog: AnthropicModelCatalog | None = None,
    managed_http_headers: dict[str, str] | None = None,
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for Claude settings.json.

    NOTE: MCP servers are NOT written here. Claude Code reads `mcpServers`
    from `~/.claude.json`, not `~/.claude/settings.json` — registration goes
    through `claude mcp add-json` (see `_register_web_search_mcp`).

    When `provider` is set (a `<catalog>.<schema>.<name>` Model Provider
    Service), the request is routed to that external provider via the
    `Databricks-Model-Provider-Service` header. An Anthropic-backed provider
    understands Claude Code's own canonical model names, so no model id is
    pinned. A Bedrock-backed provider exposes different model ids (e.g.
    `us.anthropic.claude-sonnet-4-6`), passed in `provider_models` by family —
    those get pinned via the `ANTHROPIC_DEFAULT_*_MODEL` env vars.

    When `relayed` is set (a credential-less Anthropic subscription-relay MPS,
    Claude Max/Team/Enterprise), Claude Code's own keychain OAuth must remain the
    `Authorization` credential, so no `apiKeyHelper` is written (it would outrank
    the subscription OAuth). The Databricks credential rides in the
    `X-Databricks-AI-Gateway-Token` swap header, injected per request by a local
    refresh proxy at `relayed_base_url` — not written here."""
    if relayed:
        if not relayed_base_url:
            raise RuntimeError("Relayed launch requires a proxy base URL.")
        base_url = relayed_base_url
    else:
        base_url = build_tool_base_url("claude", workspace)
    # ANTHROPIC_CUSTOM_HEADERS is parsed as `key: value` pairs separated by
    # newlines (Anthropic SDK convention). Setting User-Agent here overrides
    # the SDK's default UA on outbound requests so the gateway can attribute
    # traffic to ucode.
    header_lines = [
        "x-databricks-use-coding-agent-mode: true",
        f"User-Agent: ucode/{ug_version()} claude/{agent_version('claude')}",
    ]
    if provider:
        header_lines.append(f"{MODEL_PROVIDER_SERVICE_HEADER}: {provider}")
    elif parent_schema:
        header_lines.append(f"{MODEL_SERVICE_PARENT_SCHEMA_HEADER}: {parent_schema}")
    if smart_routing_v2.smart_routing_enabled(None, default=False, agent=AGENT_CLAUDE):
        header_lines.append(f"{SMART_ROUTER_RECIPE_HEADER}: {configured_router_name()}")
    # Relayed: the X-Databricks-AI-Gateway-Token swap header is added per request
    # by the refresh proxy, not here — a static value would go stale mid-session.
    custom_headers = "\n".join(_apply_managed_header_lines(header_lines, managed_http_headers))
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
        # 1h prompt caching needs the extended-cache-ttl beta header, which
        # Claude Code only sends when experimental betas are enabled — so we must
        # not set CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS (see CLAUDE_REMOVED_ENV_KEYS).
        "ENABLE_PROMPT_CACHING_1H": "1",
        "ENABLE_TOOL_SEARCH": "true",
        "CLAUDE_CODE_USE_GATEWAY": "1",
    }
    # Intentionally NOT setting ANTHROPIC_MODEL by default. Setting it produces a
    # duplicate catalog row in Claude Code's /model picker (e.g. "Opus 4.8 (1M
    # context) ✓") on top of the family-alias row from ANTHROPIC_DEFAULT_OPUS_MODEL.
    # Without it, Default resolves through the pinned family alias and the picker
    # shows only one row per model. `ucode claude -- --model X` still overrides for
    # a single session via Claude Code's own --model flag.
    #
    # The one exception is smart routing: `route_root_model` pins the
    # router's per-launch pick for the root session as ANTHROPIC_MODEL. The
    # duplicate-picker-row cost is acceptable because the whole point is to launch
    # on the routed model rather than the family default.
    _ = model  # API stability; no longer pinned via env.
    if route_root_model:
        env["ANTHROPIC_MODEL"] = route_root_model
    # A Bedrock-backed provider needs its provider-side ids pinned verbatim
    # (Claude Code's canonical names aren't routable there). These come from the
    # service's targets, already de-duped to one id per family upstream.
    elif provider and provider_models:
        if provider_models.get("opus"):
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = provider_models["opus"]
        if provider_models.get("sonnet"):
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = provider_models["sonnet"]
        if provider_models.get("haiku"):
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = provider_models["haiku"]
    # With an Anthropic Model Provider Service, the header routes to the external
    # provider and Claude Code's own canonical model names are sent verbatim —
    # pinning a Databricks model id here would mislabel the picker and isn't
    # routable.
    elif claude_models and not provider and not parent_schema:
        # Picker rows show the raw routable id (e.g. "system.ai.claude-opus-4-8[1m]")
        # so users can see which gateway-routable model is behind each shortcut.
        # We deliberately don't set the `_NAME` companion env vars — the raw id
        # is more useful than a friendly label for debugging gateway routing.
        for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items():
            if family_model := claude_models.get(family):
                env[key] = (
                    _maybe_add_1m_suffix(family_model)
                    if family in ("opus", "sonnet")
                    else family_model
                )
    # Relayed omits apiKeyHelper so Claude Code's subscription OAuth stays the
    # Authorization credential; every other path uses it as the gateway auth.
    overlay: dict = {"env": env}
    if relayed:
        keys = [["env", k] for k in env]
    else:
        if custom_oauth:
            overlay["apiKeyHelper"] = build_custom_auth_shell_command(workspace, custom_oauth)
        else:
            overlay["apiKeyHelper"] = build_auth_shell_command(workspace, profile, use_pat=use_pat)
        keys = [["apiKeyHelper"]] + [["env", k] for k in env]

    # Disable Claude Code's built-in WebSearch: it declares Anthropic's hosted
    # `web_search_20250305` server tool, which the Databricks gateway rejects
    # (HTTP 400: "Input tag 'web_search_20250305' ... does not match"), so the
    # model wastes a turn on it before falling back. A *bare* `permissions.deny`
    # entry removes the tool from Claude's context entirely, so it is never
    # advertised to the model nor sent to the gateway. (Claude Code has no
    # `disabledTools` setting — the `permissions` block is the only settings.json
    # mechanism for built-in tools; a bare tool name in `deny` drops it, whereas
    # a scoped rule like `WebSearch(*)` would leave it advertised.) The
    # replacement `web_search` MCP server is registered separately via the
    # claude CLI.
    if disable_web_search:
        overlay["permissions"] = {"deny": ["WebSearch"]}
        keys.append(["permissions", "deny"])

    if static_models and not provider and not parent_schema and not relayed:
        overlay["availableModels"] = list(static_models)
        overlay["enforceAvailableModels"] = True
        overlay["modelPicker"] = {
            "replaceBuiltInOptions": True,
            "options": [{"model": m, "label": _picker_label(m)} for m in static_models],
        }
        keys += [[key] for key in CLAUDE_MANAGED_PICKER_KEYS]
    elif picker_catalog and picker_catalog.model_ids and not relayed:
        overlay["modelPicker"] = {
            "replaceBuiltInOptions": True,
            "options": [
                _picker_option(
                    model,
                    picker_catalog.model_id_to_display_name.get(model) or _picker_label(model),
                    picker_catalog.model_id_to_description.get(model),
                )
                for model in picker_catalog.model_ids
            ],
        }
        keys.append(["modelPicker"])

    if otel_tracing:
        otel_env = _otel_trace_env(workspace)
        env.update(otel_env)
        overlay["otelHeadersHelper"] = build_otel_headers_shell_command(
            workspace, profile, use_pat=use_pat
        )
        keys += [["env", key] for key in otel_env] + [["otelHeadersHelper"]]

    return overlay, keys


_MODEL_LABEL_ACRONYMS = frozenset({"glm", "gpt"})
_CLAUDE_PICKER_LABEL_RE = re.compile(
    r"^Claude (Fable|Opus|Sonnet|Haiku) (\d+(?:\.\d+)*)$", re.IGNORECASE
)


def _picker_label(model: str) -> str:
    """A human-friendly picker label for a model id (e.g. ``system.ai.claude-haiku-4-5`` ->
    ``Claude Haiku 4.5``): keep the vendor and name words title-cased, uppercase known acronyms,
    and join a run of numeric segments into a dotted version."""
    stem = model.removeprefix("system.ai.")
    parts: list[str] = []
    version: list[str] = []
    for token in stem.split("-"):
        if token.isdigit():
            version.append(token)
            continue
        if version:
            parts.append(".".join(version))
            version = []
        parts.append(token.upper() if token in _MODEL_LABEL_ACRONYMS else token.title())
    if version:
        parts.append(".".join(version))
    return " ".join(parts) if parts else stem


def _picker_option(model: str, label: str, description: str | None = None) -> dict[str, str]:
    option = {"model": model, "label": label}
    if description:
        option["description"] = description
    match = _CLAUDE_PICKER_LABEL_RE.fullmatch(label)
    if match:
        family, version = match.groups()
        option["behavesAs"] = f"claude-{family.lower()}-{version.replace('.', '-')}"
    return option


def _maybe_add_1m_suffix(model: str) -> str:
    if model.endswith("[1m]"):
        return model
    match = _CLAUDE_MODEL_RE.match(model)
    if not match:
        return model

    family, major_raw, minor_raw, _ = match.groups()
    major = int(major_raw)
    minor = int(minor_raw or 0)
    should_suffix = (family == "opus" and (major, minor) >= (4, 6)) or (
        family == "sonnet" and (major, minor) >= (4, 6)
    )
    return f"{model}[1m]" if should_suffix else model


def default_model_picker_catalog(
    defaults: dict[str, str],
    *,
    provider: str | None = None,
    launch_model: str | None = None,
    discovered_catalog: AnthropicModelCatalog | None = None,
) -> AnthropicModelCatalog:
    """Build a replacement picker catalog from managed defaults and discovered models."""

    model_ids: list[str] = []
    display_names: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for family, raw_model in defaults.items():
        model = raw_model
        label = _picker_label(model.removesuffix("[1m]"))
        if provider is not None:
            # Family shortcuts stay distinct from catalog rows for the same target.
            model = family
            label = f"Default {family.title()}"
            descriptions[model] = raw_model
        elif family in ("opus", "sonnet"):
            # Match the current model's exact id so Claude does not append a duplicate row.
            if launch_model and model.removesuffix("[1m]") == launch_model.removesuffix("[1m]"):
                model = launch_model
            else:
                model = _maybe_add_1m_suffix(model)
        if model in model_ids:
            continue
        model_ids.append(model)
        display_names[model] = label

    if discovered_catalog is not None:
        for model in discovered_catalog.model_ids:
            if model not in model_ids:
                model_ids.append(model)
            if label := discovered_catalog.model_id_to_display_name.get(model):
                display_names[model] = label
            if description := discovered_catalog.model_id_to_description.get(model):
                descriptions[model] = description

    return AnthropicModelCatalog(
        model_ids=model_ids,
        model_id_to_display_name=display_names,
        model_id_to_description=descriptions,
        error_msg=discovered_catalog.error_msg if discovered_catalog is not None else None,
    )


def _enforce_model_default_hierarchy(
    family: str,
    *,
    coding_agent_config_defaults: dict[str, str],
    settings_file_existing_defaults: dict[str, str],
    ucode_defaults: dict[str, str],
    ucode_last_written_defaults: dict[str, str],
    enforced_models: list[str] | None,
    add_1m_suffix: bool = True,
) -> str | None:
    """Resolve one Claude family's managed-file default model.

    An existing managed-file default ucode did not write itself (it differs from ucode's last write)
    is an administrator's, so it is preserved verbatim. Otherwise the value is ucode's own or unset,
    so ucode re-derives it from the coding-agent config, then discovery, resetting a value carried
    over from a previous workspace, and drops the result when an enforced model list excludes it.
    """
    selected = coding_agent_config_defaults.get(family)
    if selected is None:
        existing = settings_file_existing_defaults.get(family)
        if existing is not None and existing != ucode_last_written_defaults.get(family):
            return existing
        selected = ucode_defaults.get(family)
    if selected is None:
        return None
    if add_1m_suffix and family in ("opus", "sonnet"):
        selected = _maybe_add_1m_suffix(selected)
    if enforced_models is not None and selected.split("[", 1)[0] not in enforced_models:
        return None
    return selected


def add_claude_mcp_server(
    name: str,
    server: list[str] | dict,
    scope: str = MCP_USER_SCOPE,
    *,
    always_load: bool = False,
) -> None:
    # Three registration shapes share this helper. The plain proxy path passes an
    # argv list (`ug mcp-proxy ...`), registered via `claude mcp add ... -- <argv>`
    # where `--` fences the proxy's own flags off from claude's parser. The
    # web_search server passes a full stdio entry dict with its own env, which only
    # `add-json` can express — so a dict routes there. Finally, `always_load` (the
    # skills registry) needs `alwaysLoad: true`, which plain `mcp add` can't set, so
    # build a stdio entry dict and route it to add-json too.
    if isinstance(server, dict):
        cmd = ["claude", "mcp", "add-json", name, json.dumps(server), "-s", scope]
    elif always_load:
        entry = {
            "type": "stdio",
            "command": server[0],
            "args": list(server[1:]),
            "alwaysLoad": True,
        }
        cmd = ["claude", "mcp", "add-json", name, json.dumps(entry), "-s", scope]
    else:
        cmd = ["claude", "mcp", "add", name, "-s", scope, "--", *server]
    try:
        subprocess_cross_os.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to add MCP server '{name}' via claude CLI.") from exc


def add_claude_http_mcp_server(
    name: str,
    url: str,
    scope: str = MCP_USER_SCOPE,
    *,
    client_id: str = CLAUDE_CODE_OAUTH_CLIENT_ID,
    callback_port: int = MCP_OAUTH_CALLBACK_PORT,
) -> None:
    """Register a Databricks MCP endpoint as a **direct HTTP** server so Claude
    Code is the OAuth client and drives the RFC 8707 connection login itself.

    Unlike the stdio proxy (which injects a plain workspace token and hides the
    per-user connection state), a direct HTTP server lets Claude Code do MCP OAuth
    against ``/oidc`` with the ``resource`` indicator: on a missing/expired
    connection credential, ``/mcp`` shows "needs authentication" and Authenticate
    runs the login (``/oidc`` -> ``/mcp-service-login``). ``client_id`` is the
    published ``claude-code`` app (it has the loopback ``/callback`` redirect
    registered); the callback port is arbitrary because ``/oidc`` ignores the port
    for loopback redirects."""
    cmd = [
        "claude",
        "mcp",
        "add",
        "--transport",
        "http",
        "-s",
        scope,
        "--client-id",
        client_id,
        "--callback-port",
        str(callback_port),
        name,
        url,
    ]
    try:
        subprocess_cross_os.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to add HTTP MCP server '{name}' via claude CLI.") from exc


def remove_claude_mcp_server(name: str, scope: str) -> bool:
    # Imported lazily: `_is_missing_mcp_server_output` is a shared CLI-output matcher
    # in ucode.mcp (used by the codex/gemini removers too), and ucode.mcp imports
    # this module at load time — a function-level import avoids that cycle.
    from ucode.mcp import _is_missing_mcp_server_output

    try:
        subprocess_cross_os.run(
            ["claude", "mcp", "remove", name, "-s", scope],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except subprocess.CalledProcessError as exc:
        output = f"{exc.stderr or ''}\n{exc.stdout or ''}"
        if _is_missing_mcp_server_output(output):
            return False
        raise RuntimeError(f"Failed to remove MCP server '{name}' via claude CLI.") from exc


def user_stdio_mcp_entry(argv: list[str], *, always_load: bool = False) -> dict:
    """The user-scope ``mcpServers`` entry that ``claude mcp add ... -- <argv>`` writes.

    Mirrors the CLI's on-disk shape so a batched direct write is what the CLI would have produced:
    a plain stdio server carries an empty ``env`` map, while the skills registry's ``alwaysLoad``
    entry carries that flag instead (as ``add-json`` writes it)."""
    entry: dict = {"type": "stdio", "command": argv[0], "args": list(argv[1:])}
    if always_load:
        entry["alwaysLoad"] = True
    else:
        entry["env"] = {}
    return entry


def claude_mcp_config_path() -> Path:
    """The file Claude Code reads user-scope ``mcpServers`` from: ``$CLAUDE_CONFIG_DIR/.claude.json``
    when that env var is set (the ``claude`` CLI honors it), else the default ``~/.claude.json``. ug
    elsewhere shells out to the CLI, which resolves this itself; a direct write must resolve the same
    path or it silently writes to a file Claude never reads."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(config_dir) / ".claude.json" if config_dir else CLAUDE_MCP_CONFIG_PATH


def _read_claude_config_for_rewrite(path: Path) -> dict | None:
    """Read ``path`` for a full rewrite: ``{}`` when absent, the parsed object when present and
    valid, and ``None`` when present but not a parseable JSON object — so a caller never overwrites
    (and destroys) a config it could not read."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Claude's user-scope ``mcpServers`` (``~/.claude.json``, or under
    ``$CLAUDE_CONFIG_DIR``) in a single read-modify-write, instead of one ``claude mcp`` subprocess
    per server (each ~0.3-0.8s; a large managed set is otherwise dozens of them run serially). The
    developer's own servers and every other key in the file are preserved. Returns the subset of
    ``remove`` names that were actually present (so callers can report only real removals).

    If the file exists but can't be parsed as a JSON object, we must not clobber it, so we defer to
    the per-server ``claude`` CLI (which edits the file in place) for exactly the changed entries."""
    path = claude_mcp_config_path()
    config = _read_claude_config_for_rewrite(path)
    if config is None:
        removed: set[str] = set()
        for name in remove:
            # Clean every scope (not short-circuited), recording the name if any scope had it.
            if [scope for scope in MCP_CLEANUP_SCOPES if remove_claude_mcp_server(name, scope)]:
                removed.add(name)
        for name, entry in add.items():
            if entry.get("type") == "http":
                oauth = entry.get("oauth") or {}
                add_claude_http_mcp_server(
                    name,
                    entry["url"],
                    client_id=oauth.get("clientId", CLAUDE_CODE_OAUTH_CLIENT_ID),
                    callback_port=oauth.get("callbackPort", MCP_OAUTH_CALLBACK_PORT),
                )
            else:
                add_claude_mcp_server(name, entry, MCP_USER_SCOPE)
        return removed

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    removed = {name for name in remove if name in servers}
    for name in remove:
        servers.pop(name, None)
    servers.update(add)
    config["mcpServers"] = servers
    write_json_file(path, config)
    return removed


def managed_mcp_uses_managed_file(workspace: str, *, use_pat: bool) -> bool:
    """Whether Claude's managed MCP servers belong in the OS-managed file rather than user scope.

    The OS-managed ``managedMcpServers`` key is additive (it never touches the developer's own
    servers) but Claude Code reads it only from a real managed source, and only as a remote HTTP
    server it can drive OAuth against itself. So it fits only when the platform supports the sudo
    reconcile, the run is interactive, the developer is not on PAT auth (which needs the stdio
    proxy), and the workspace publishes the ``claude-code`` OAuth client. Every other case falls back to the user-scope registration."""
    return (
        managed_files_supported()
        and managed_writes_allowed()
        and not use_pat
        and oauth_client_available(workspace, CLAUDE_CODE_OAUTH_CLIENT_ID)
    )


def managed_mcp_entry(url: str) -> dict:
    """A ``managedMcpServers`` entry: a direct HTTP server Claude Code drives OAuth against itself.

    Mirrors :func:`add_claude_http_mcp_server`: the published ``claude-code`` OAuth client and an
    arbitrary loopback callback port, which ``/oidc`` ignores for loopback redirects."""
    return {
        "type": "http",
        "url": url,
        "oauth": {
            "clientId": CLAUDE_CODE_OAUTH_CLIENT_ID,
            "callbackPort": MCP_OAUTH_CALLBACK_PORT,
        },
    }


def reconcile_managed_mcp(
    state: dict, servers: dict[str, dict], *, also_registered: Iterable[str] = ()
) -> bool:
    """Overwrite ug's ``managedMcpServers`` in Claude's OS-managed file with ``servers``.

    ``servers`` is the freshly resolved managed set keyed by name; an empty map clears the key. The
    managed file is the source of truth, so this is a wipe-and-rewrite, not a diff. Every other
    managed key is preserved, including the model configuration ug wrote earlier this run and any
    admin-authored policy. Returns True when the managed file is the delivery mechanism (written or
    already current), False when it cannot be used (unsupported platform or a non-interactive run),
    so the caller routes those servers to the user-scope registration instead.

    ``also_registered`` names the servers ug registers in Claude outside this file this run (the
    user-scope fallback), so an admin's ``agent_native_settings`` MCP allowlist keeps admitting them.
    """
    path = _managed_settings_path()
    if path is None or not managed_writes_allowed():
        return False
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use Claude Code managed settings through symlink {path}. Replace it "
            "with a regular file or contact your administrator."
        )
    current_text = read_managed_file(path)
    try:
        existing = _parse_managed_settings(current_text) if current_text is not None else {}
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely update Claude Code managed settings at {path}: {exc}. ucode did not "
            "modify the file. Repair it or contact your administrator."
        ) from exc
    desired = copy.deepcopy(existing)
    if servers:
        desired[MANAGED_MCP_SETTINGS_KEY] = servers
    else:
        desired.pop(MANAGED_MCP_SETTINGS_KEY, None)
    admin_allowlist = _delivered_admin_allowlist()
    desired = _allow_ug_mcp_servers(
        desired,
        admin_allowlist,
        _ug_registered_mcp_names(
            state,
            servers,
            web_search=bool(state.get(WEB_SEARCH_MCP_STATE_KEY)),
            also=also_registered,
        ),
    )
    # Nothing to change: never create or rewrite the file just to restate it.
    if desired == existing:
        return True
    owned_paths = [[MANAGED_MCP_SETTINGS_KEY]]
    if admin_allowlist is not None:
        owned_paths.append([CLAUDE_ALLOWED_MCP_SERVERS_KEY])
    try:
        reconcile_managed_file(
            path,
            _dump_managed_settings(desired),
            tool="claude",
            display="Claude Code",
            owned_paths=owned_paths,
            parser=_parse_managed_settings,
        )
    except ManagedFileWriteUnavailable:
        return False
    # Preserve the scope the model reconcile recorded (e.g. relay-compatible); an MCP-only write only
    # refreshes the fingerprint, it does not change how the file relates to the model settings.
    mark_managed_file_verified(state, "claude", path, scope=managed_file_scope(state, "claude"))
    return True


def read_managed_mcp_urls() -> dict[str, str]:
    """``{name: url}`` for ug's managed MCP servers in Claude's OS-managed file (empty if none).

    Read-only, for ``ug mcp list`` to tag managed servers now that the managed file is their source
    of truth rather than ug state."""
    path = _managed_settings_path()
    if path is None:
        return {}
    try:
        text = read_managed_file(path)
        settings = _parse_managed_settings(text) if text else {}
    except RuntimeError:
        return {}
    servers = settings.get(MANAGED_MCP_SETTINGS_KEY)
    if not isinstance(servers, dict):
        return {}
    return {
        name: entry["url"]
        for name, entry in servers.items()
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    }


def _register_web_search_mcp(
    workspace: str,
    search_model: str,
    profile: str | None = None,
    *,
    previous_entry: object = None,
) -> bool:
    """Register (or replace) the web_search MCP server in Claude Code's user
    scope via `claude mcp add-json`. Replace only a known generated user entry;
    project/local entries and user edits belong to the caller.

    Returns True if registration succeeded. Failures are non-blocking: we warn
    and return False so the rest of `ucode claude` setup can complete.
    """
    config = _read_claude_config_for_rewrite(claude_mcp_config_path())
    servers = config.get("mcpServers", {}) if config is not None else None
    existing = servers.get(WEB_SEARCH_MCP_NAME) if isinstance(servers, dict) else None
    if not isinstance(servers, dict) or (
        existing is not None
        and (existing != previous_entry or not _generated_search_entry(existing))
    ):
        print_warning(
            "Preserving web_search: its Claude configuration is unreadable or its entry is not "
            "an unchanged ug-generated registration. Review it before reconfiguring search."
        )
        return False
    try:
        remove_claude_mcp_server(WEB_SEARCH_MCP_NAME, MCP_USER_SCOPE)
    except RuntimeError:
        pass
    entry = _web_search_mcp_entry(workspace, search_model, profile)
    try:
        add_claude_mcp_server(WEB_SEARCH_MCP_NAME, entry)
    except RuntimeError as exc:
        print_warning(f"{exc} Web search will be unavailable; re-run `ucode claude` to retry.")
        return False
    return True


def _web_search_mcp_is_current(state: dict, entry: dict) -> bool:
    """Return whether the desired web-search entry is already registered.

    The persisted entry acts as a cheap fingerprint, while reading Claude's config repairs a
    registration removed or edited outside ucode. Avoiding the Claude CLI here matters: each
    ``claude mcp`` subprocess takes roughly 0.8 seconds during a launch.
    """
    if state.get(WEB_SEARCH_MCP_STATE_KEY) != entry:
        return False
    config = read_json_safe(claude_mcp_config_path())
    servers = config.get("mcpServers")
    return isinstance(servers, dict) and servers.get(WEB_SEARCH_MCP_NAME) == entry


def _external_search_conflict(reason: str) -> RuntimeError:
    return RuntimeError(
        f"Cannot safely select external web search: {reason}. No search registration was changed. "
        "Review the web_search MCP entry and its ug ownership state, or launch with "
        f"{PROVIDER_ENV}=ucode to keep the existing provider."
    )


def _search_config(path: Path) -> dict:
    config = _read_claude_config_for_rewrite(path)
    if config is None:
        raise _external_search_conflict(f"cannot read a JSON object at {path}")
    return config


def _search_servers(config: dict, key: str = "mcpServers") -> dict:
    servers = config.get(key, {})
    if not isinstance(servers, dict):
        raise _external_search_conflict(f"{key} is not an object")
    return servers


def _generated_search_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"type", "command", "args", "env"}:
        return False
    command = entry.get("command")
    env = entry.get("env")
    return (
        entry["type"] == "stdio"
        and isinstance(command, str)
        and Path(command).name.lower() in ("ug", "ucode", "ug.exe", "ucode.exe")
        and entry["args"] in (["mcp", "web-search"], ["mcp", "web-search", MANAGED_ENTRY_FLAG])
        and isinstance(env, dict)
        and all(isinstance(value, str) for value in env.values())
        and bool(env.get("DATABRICKS_HOST"))
        and bool(env.get("UCODE_WEB_SEARCH_MODEL"))
        and set(env)
        <= {
            "DATABRICKS_HOST",
            "UCODE_WEB_SEARCH_MODEL",
            "DATABRICKS_CONFIG_PROFILE",
        }
    )


def _external_web_search_args(state: dict, tool_args: list[str]) -> list[str]:
    if not external_provider_selected():
        return tool_args
    try:
        return _prepare_external_web_search_args(state, tool_args)
    except RuntimeError as exc:
        if os.environ.get(PROVIDER_ENV) != AUTOMATIC_PROVIDER:
            raise
        print_warning(
            f"{exc} Keeping the existing provider for this launch; duplicates may remain."
        )
        os.environ[PROVIDER_ENV] = "ucode"
        return tool_args


def _prepare_external_web_search_args(state: dict, tool_args: list[str]) -> list[str]:
    """Shadow only a verified ug registration, without rewriting shared MCP configuration.

    A launch-scoped helper also handles legacy entries pointing at a different ug install.
    Standalone launches keep their original registration while Isaac is running.
    """
    end = tool_args.index("--") if "--" in tool_args else len(tool_args)
    options = tool_args[:end]
    if "--strict-mcp-config" in options:
        return tool_args
    config = _search_config(claude_mcp_config_path())
    entry = _search_servers(config).get(WEB_SEARCH_MCP_NAME)
    if entry is None:
        return tool_args

    # Preserve the user's disabled registration without introducing a dynamic scope.
    directories = [Path.cwd(), *Path.cwd().parents]
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise _external_search_conflict("Claude's projects configuration is not an object")
    local_configs = []
    for directory in directories:
        local = projects.get(str(directory), {})
        if not isinstance(local, dict):
            raise _external_search_conflict(
                f"project configuration at {directory} is not an object"
            )
        disabled = local.get("disabledMcpServers", [])
        if not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled):
            raise _external_search_conflict(f"invalid disabledMcpServers at {directory}")
        if WEB_SEARCH_MCP_NAME in disabled:
            return tool_args
        local_configs.append(local)
    if not _generated_search_entry(entry) or entry != state.get(WEB_SEARCH_MCP_STATE_KEY):
        raise _external_search_conflict(
            "the user-scope web_search entry has unknown or edited ownership"
        )

    for option in options:
        if option in ("--worktree", "-w", "--setting-sources") or option.startswith(
            ("--worktree=", "--setting-sources=")
        ):
            raise _external_search_conflict(
                "custom project/config scope prevents ownership verification"
            )

    for directory, local in zip(directories, local_configs, strict=True):
        if WEB_SEARCH_MCP_NAME in _search_servers(local):
            raise _external_search_conflict(f"a local web_search entry exists at {directory}")
        if WEB_SEARCH_MCP_NAME in _search_servers(_search_config(directory / ".mcp.json")):
            raise _external_search_conflict(f"a project web_search entry exists at {directory}")

    managed_path = _managed_settings_path()
    if managed_path is None:
        raise _external_search_conflict("managed MCP scope cannot be inspected on this platform")
    if managed_path.with_name("managed-mcp.json").exists():
        raise _external_search_conflict("managed-mcp.json has exclusive control over MCP servers")
    user_settings = (
        Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
        if os.environ.get("CLAUDE_CONFIG_DIR")
        else CLAUDE_USER_SETTINGS_PATH
    )
    settings_paths = [managed_path, user_settings, CLAUDE_SETTINGS_PATH]
    settings_paths.extend(sorted(managed_path.with_name("managed-settings.d").glob("*.json")))
    for directory in directories:
        settings_paths.extend(
            [directory / ".claude/settings.json", directory / ".claude/settings.local.json"]
        )
    settings = [_search_config(path) for path in settings_paths]
    caller_settings, _ = _extract_caller_settings(options)
    settings.extend(_load_caller_settings(value) for value in caller_settings)
    for setting in settings:
        if WEB_SEARCH_MCP_NAME in _search_servers(setting, MANAGED_MCP_SETTINGS_KEY):
            raise _external_search_conflict("a managed web_search entry exists")
        # Replacing an executable must never evade a command-based MCP policy.
        if "deniedMcpServers" in setting or "allowedMcpServers" in setting:
            raise _external_search_conflict("MCP policy requires review before replacing a helper")
        env = setting.get("env", {})
        if not isinstance(env, dict) or env.get(PROVIDER_ENV, "external") != "external":
            raise _external_search_conflict("Claude settings override the selected search provider")

    last_mcp_option = None
    i = 0
    while i < len(options):
        option = options[i]
        values = []
        if option == "--mcp-config":
            last_mcp_option = i
            i += 1
            while i < len(options) and not options[i].startswith("-"):
                values.append(options[i])
                i += 1
            if not values:
                raise _external_search_conflict("--mcp-config has no value")
        elif option.startswith("--mcp-config="):
            last_mcp_option = i
            values.append(option.partition("=")[2])
            i += 1
        else:
            i += 1
        for value in values:
            caller_config = _load_caller_settings(value)
            if WEB_SEARCH_MCP_NAME in _search_servers(caller_config):
                raise _external_search_conflict("--mcp-config defines its own web_search entry")

    override = {
        "type": "stdio",
        "command": sys.executable,
        "args": [
            "-m",
            "ucode.cli",
            "mcp",
            "web-search",
            EXTERNAL_PROVIDER_OVERRIDE_FLAG,
            MANAGED_ENTRY_FLAG,
        ],
        "env": {**entry["env"], PROVIDER_ENV: "external"},
    }
    value = json.dumps({"mcpServers": {WEB_SEARCH_MCP_NAME: override}})
    result = list(tool_args)
    if last_mcp_option is None:
        result[end:end] = ["--mcp-config", value]
    else:
        option = result[last_mcp_option]
        if option.startswith("--mcp-config="):
            result[last_mcp_option : last_mcp_option + 1] = [
                "--mcp-config",
                value,
                option.partition("=")[2],
            ]
        else:
            result.insert(last_mcp_option + 1, value)
    return result


def _unregister_web_search_mcp() -> None:
    """Remove the web_search MCP server from all scopes. Used by revert."""
    for scope in MCP_CLEANUP_SCOPES:
        try:
            remove_claude_mcp_server(WEB_SEARCH_MCP_NAME, scope)
        except RuntimeError:
            pass


def disable_smart_routing(state: dict) -> bool:
    """Disable routing and remove only ucode's Claude Code routing hooks."""
    state.pop(SMART_ROUTING_STATE_KEY, None)
    if state.get("workspace"):
        save_state(state)
    changed = False
    if CLAUDE_SETTINGS_PATH.exists():
        doc = read_json_safe(CLAUDE_SETTINGS_PATH)
        if remove_smart_routing_hooks(doc):
            write_json_file(CLAUDE_SETTINGS_PATH, doc)
            changed = True
    from ucode.smart_routing.claude_routing import clear_routing_artifacts

    clear_routing_artifacts()
    return changed


def write_tool_config(
    state: dict,
    model: str | None,
    provider: str | None = None,
    provider_models: dict[str, str] | None = None,
    relayed: bool = False,
    route_root_model: str | None = None,
    custom_model: str | None = None,
    coding_agent_config_defaults: dict[str, str] | None = None,
    parent_schema: str | None = None,
    picker_catalog: AnthropicModelCatalog | None = None,
) -> dict:
    external_search = external_provider_selected()
    # Back up only a file that predates ucode's management of the tool. A
    # re-configure would otherwise snapshot ucode's own generated file, and
    # revert would restore that snapshot instead of deleting the file.
    if not is_tool_managed(state, "claude"):
        backup_existing_file(CLAUDE_SETTINGS_PATH, CLAUDE_BACKUP_PATH)
    # A managed config makes ug authoritative over the whole custom-header value, so it is
    # overwritten wholesale; without one, preserve the developer's own pre-existing headers. Reuses
    # this launch's warm managed-config cache (no extra round trip); a failed fetch degrades to None
    # (treated as unmanaged), never blocking the write.
    managed_config_present = refresh_managed_config(state).manifest is not None
    previous_keys = ((state.get("managed_configs") or {}).get("claude") or {}).get("keys", [])
    web_search_model = _resolve_web_search_model(state)
    should_write_tracing_settings = (
        bool(state.get("claude_otel_tracing")) and managed_config_present
    )
    # Relayed inference points at a local refresh proxy; its loopback base URL is
    # recorded in state so launch starts the proxy on the matching port.
    relayed_base_url = relayed_proxy_base_url(state) if relayed else None
    overlay, managed_keys = render_overlay(
        state["workspace"],
        model,
        state.get("claude_models") or {},
        disable_web_search=web_search_model is not None,
        profile=state.get("profile"),
        use_pat=bool(state.get("use_pat")),
        custom_oauth=state.get("custom_oauth"),
        provider=provider,
        provider_models=provider_models,
        relayed=relayed,
        relayed_base_url=relayed_base_url,
        route_root_model=route_root_model,
        custom_model=custom_model,
        parent_schema=parent_schema,
        static_models=state.get("claude_static_models"),
        otel_tracing=should_write_tracing_settings,
        picker_catalog=picker_catalog,
        managed_http_headers=state.get("claude_http_headers"),
    )
    source_scoped_defaults = bool((provider or parent_schema) and coding_agent_config_defaults)
    # Native discovery must not inherit UG's prior static allow-list. Keep a replacement picker
    # written by this launch, and remove only previously owned picker keys that no longer apply.
    stale_picker_keys = [
        key
        for key in CLAUDE_MANAGED_PICKER_KEYS
        if [key] in previous_keys and key not in overlay and (provider or parent_schema)
    ]
    managed_file_keys = list(managed_keys)
    for path in (
        [[key] for key in stale_picker_keys]
        + [["env", key] for key in CLAUDE_MANAGED_MODEL_ENV_KEYS]
        + [["env", key] for key in CLAUDE_CONDITIONAL_ENV_KEYS]
        + [["env", key] for key in CLAUDE_REMOVED_ENV_KEYS]
        + [["env", key] for key in CLAUDE_OTEL_TRACE_ENV_KEYS]
        + [["otelHeadersHelper"]]
        + [["hooks", event] for event in ("PreToolUse", "SessionStart", "SubagentStart")]
    ):
        if path not in managed_file_keys:
            managed_file_keys.append(path)

    # V2 installs routing hooks in a transient per-launch settings file. Persistent settings must
    # contain no ucode routing hooks; surgically strip legacy ones while preserving user hooks.
    def _compose(
        base: dict,
        *,
        enforce_model_default_hierarchy: bool,
        managed_settings_snapshots: ManagedFileSnapshots | None,
    ) -> dict:
        base_env = base.get("env")
        existing_custom_headers = (
            base_env.get(ANTHROPIC_CUSTOM_HEADERS_ENV_KEY) if isinstance(base_env, dict) else None
        )
        # Copy the overlay per file so merging into one base cannot affect the other.
        overlay_for_merge = copy.deepcopy(overlay)
        should_preserve_managed_tracing = (
            managed_settings_snapshots is not None and not should_write_tracing_settings
        )
        # UG only owns managed-file telemetry while a workspace config actively enables it.
        # Otherwise omit these paths from the overlay so the live managed values pass through.
        if should_preserve_managed_tracing:
            prune_key_paths(overlay_for_merge, [list(path) for path in CLAUDE_OTEL_TRACE_PATHS])
        if enforce_model_default_hierarchy:
            settings_file_env = base_env if isinstance(base_env, dict) else {}
            target_env = overlay_for_merge["env"]
            configured_defaults = coding_agent_config_defaults or {}
            if source_scoped_defaults:
                # The managed map is complete policy for this source: omitted families must not
                # inherit targets from local settings or live discovery.
                settings_file_existing_defaults = {}
                ucode_defaults = {}
            else:
                settings_file_existing_defaults = {
                    family: model
                    for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items()
                    if isinstance((model := settings_file_env.get(key)), str)
                }
                managed_overlay = state.get(MANAGED_OVERLAY_KEY, {})
                ucode_defaults = (
                    managed_overlay.get("claude_models") or state.get("claude_models") or {}
                )

            enforced_models = overlay_for_merge.get("availableModels")
            last_applied_env = {}
            if (
                managed_settings_snapshots is not None
                and managed_settings_snapshots.last_applied_by_ug
            ):
                last_applied_env = managed_settings_snapshots.last_applied_by_ug.get("env") or {}
            ucode_last_written_defaults = {
                family: last_applied_env[key]
                for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items()
                if isinstance(last_applied_env.get(key), str)
            }
            for family, key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.items():
                selected_default_model = _enforce_model_default_hierarchy(
                    family,
                    coding_agent_config_defaults=configured_defaults,
                    settings_file_existing_defaults=settings_file_existing_defaults,
                    ucode_defaults=ucode_defaults,
                    ucode_last_written_defaults=ucode_last_written_defaults,
                    enforced_models=enforced_models,
                    add_1m_suffix=provider is None,
                )
                if selected_default_model is None:
                    target_env.pop(key, None)
                else:
                    target_env[key] = selected_default_model
        should_preserve_preexisting_claude_family_defaults = (
            not managed_config_present
            and not coding_agent_config_defaults
            and isinstance(base_env, dict)
        )
        if should_preserve_preexisting_claude_family_defaults:
            # Without an admin Coding Agent Config, an existing family default belongs to the
            # developer. Discovery may fill an absent family, but must not replace a value they
            # already selected in either the private or OS-managed settings file.
            target_env = overlay_for_merge["env"]
            for key in CLAUDE_DEFAULT_MODEL_ENV_KEYS.values():
                existing_default = base_env.get(key)
                if isinstance(existing_default, str):
                    target_env[key] = existing_default
        merged = deep_merge_dict(base, overlay_for_merge)
        for key in stale_picker_keys:
            merged.pop(key, None)
        overlay_custom_headers = overlay_for_merge["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY]
        if managed_config_present:
            # ug owns the whole value under a managed config: overwrite wholesale so a header ug no
            # longer emits is dropped and no stale or foreign header lingers.
            merged["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY] = overlay_custom_headers
        else:
            # No managed config: preserve the developer's own pre-existing headers, replacing only
            # the header names ug manages.
            merged["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY] = _merge_anthropic_custom_headers(
                existing_custom_headers, overlay_custom_headers
            )
        # Drop any apiKeyHelper a prior non-relayed launch left in the file; relayed
        # must not carry one (it would outrank the subscription OAuth).
        if relayed:
            merged.pop("apiKeyHelper", None)
        # Prune ucode-managed model env keys we deliberately don't write this run
        # (e.g. ANTHROPIC_MODEL — see render_overlay).
        overlay_env = overlay_for_merge.get("env", {})
        merged_env = merged.get("env")
        if isinstance(merged_env, dict):
            for key in CLAUDE_MANAGED_MODEL_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            for key in CLAUDE_CONDITIONAL_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            # deep_merge_dict keeps keys already in the file, so drop the ones ucode no
            # longer writes.
            for key in CLAUDE_REMOVED_ENV_KEYS:
                merged_env.pop(key, None)
        if managed_settings_snapshots is None and not should_write_tracing_settings:
            prune_key_paths(merged, [list(path) for path in CLAUDE_OTEL_TRACE_PATHS])
        if not any(key in overlay_for_merge for key in CLAUDE_MANAGED_PICKER_KEYS):
            if managed_settings_snapshots is None:
                for key in CLAUDE_MANAGED_PICKER_KEYS:
                    merged.pop(key, None)
            else:
                # Revert only the picker ug recorded writing, and only while it is untouched as a
                # unit; last-applied snapshots also hold foreign pickers ug merely preserved.
                ug_picker = managed_settings_snapshots.ug_picker or {}
                if ug_picker and all(merged.get(key) == ug_picker[key] for key in ug_picker):
                    baseline = managed_settings_snapshots.original_before_ug or {}
                    for key in ug_picker:
                        if key in baseline:
                            merged[key] = baseline[key]
                        else:
                            merged.pop(key, None)
                elif not ug_picker:
                    _warn_unrecorded_allow_list(merged, managed_settings_snapshots)
        sync_smart_routing_hooks(merged, state, enabled=False)
        return merged

    managed_snapshots = managed_file_snapshots("claude", _parse_managed_settings)
    write_json_file(
        CLAUDE_SETTINGS_PATH,
        _compose(
            read_json_safe(CLAUDE_SETTINGS_PATH),
            enforce_model_default_hierarchy=source_scoped_defaults,
            managed_settings_snapshots=None,
        ),
    )

    # The admin's settings may not displace anything ug writes, except the hooks and deny lists ug
    # only merges its own entries into.
    passthrough = plan_settings_passthrough(
        state.get(SETTINGS_PASSTHROUGH_STATE_KEY),
        reserved_paths=[
            *(
                path
                for path in managed_file_keys
                if path[0] != "hooks" and path != CLAUDE_PERMISSIONS_DENY_PATH
            ),
            *([key] for key in CLAUDE_MANAGED_PICKER_KEYS),
            [MANAGED_MCP_SETTINGS_KEY],
        ],
    )
    warn_skipped_settings_passthrough(
        state, SETTINGS_PASSTHROUGH_IGNORED_STATE_KEY, passthrough, "Claude Code", print_warning
    )
    previous_leaves = (managed_snapshots.settings_passthrough if managed_snapshots else None) or []
    _warn_if_settings_disable_smart_routing(passthrough, previous_leaves)
    _reconcile_managed_settings(
        state,
        lambda base: _allow_ug_mcp_servers(
            apply_settings_passthrough(
                _compose(
                    base,
                    enforce_model_default_hierarchy=(
                        source_scoped_defaults or (provider is None and parent_schema is None)
                    ),
                    managed_settings_snapshots=managed_snapshots,
                ),
                passthrough,
                previous_leaves=previous_leaves,
                snapshots=managed_snapshots,
                is_shared_list=_is_shared_claude_list,
            ),
            passthrough.items_at([CLAUDE_ALLOWED_MCP_SERVERS_KEY])
            if [CLAUDE_ALLOWED_MCP_SERVERS_KEY] in passthrough.paths
            else None,
            _ug_registered_mcp_names(
                state,
                base.get(MANAGED_MCP_SETTINGS_KEY),
                web_search=bool(web_search_model and not external_search),
            ),
        ),
        [*managed_file_keys, *passthrough.paths],
        relayed,
        [key for key in CLAUDE_MANAGED_PICKER_KEYS if key in overlay],
        withdrawn_denies=withdrawn_list_items(
            CLAUDE_PERMISSIONS_DENY_PATH, passthrough, previous_leaves
        ),
        passthrough_leaves=passthrough.record(),
    )

    custom_oauth = state.get("custom_oauth")
    web_search_profile = (
        custom_oauth.get("profile")
        if isinstance(custom_oauth, dict) and custom_oauth.get("profile")
        else state.get("profile")
    )
    # Ownership describes the installed registration, even when no search model is available.
    if web_search_model and not external_search:
        web_search_entry = _web_search_mcp_entry(
            state["workspace"],
            web_search_model,
            web_search_profile,
        )
        if not _web_search_mcp_is_current(state, web_search_entry):
            # Registration runs multiple `claude mcp` subprocesses and can take several seconds.
            registration_success = _register_web_search_mcp(
                state["workspace"],
                web_search_model,
                web_search_profile,
                previous_entry=state.get(WEB_SEARCH_MCP_STATE_KEY),
            )
            if registration_success:
                state[WEB_SEARCH_MCP_STATE_KEY] = web_search_entry

    # Persist relayed mode + proxy port so launch() wires the refresh proxy and
    # subscription login; cleared on a non-relayed launch.
    if relayed:
        state["claude_relayed"] = True
    else:
        state.pop("claude_relayed", None)
        state.pop("relayed_proxy_port", None)
    state = mark_tool_managed(state, "claude", managed_keys)
    save_state(state)
    return state


def _merge_anthropic_custom_headers(existing: object, ucode_headers: str) -> str:
    """Preserve user headers while replacing the header names managed by ucode.

    Claude's ``ANTHROPIC_CUSTOM_HEADERS`` value is a newline-delimited string. To merge it, we:

    1. Split the existing custom headers by newline into individual header items.
    2. Split each item on ``:`` to identify its header name.
    3. Replace headers in ``CLAUDE_MANAGED_CUSTOM_HEADER_NAMES`` with ucode's values in their
       existing positions, while preserving all other existing headers.
    4. Append any ucode-managed headers that were not already present.

    Header names are compared case-insensitively. Non-header lines are also preserved to avoid
    silently discarding user configuration we do not understand.
    """

    if not isinstance(existing, str) or not existing:
        return ucode_headers

    ucode_lines_by_name: dict[str, str] = {}
    ucode_header_names: list[str] = []
    for line in ucode_headers.splitlines():
        name, separator, _value = line.partition(":")
        normalized_name = name.strip().casefold()
        if separator and normalized_name not in ucode_lines_by_name:
            ucode_header_names.append(normalized_name)
        if separator:
            ucode_lines_by_name[normalized_name] = line

    merged: list[str] = []
    replaced_names: set[str] = set()
    for line in existing.splitlines():
        name, separator, _value = line.partition(":")
        normalized_name = name.strip().casefold()
        if separator and normalized_name in CLAUDE_MANAGED_CUSTOM_HEADER_NAMES:
            replacement = ucode_lines_by_name.get(normalized_name)
            if replacement is not None and normalized_name not in replaced_names:
                merged.append(replacement)
                replaced_names.add(normalized_name)
            continue
        if line:
            merged.append(line)

    for name in ucode_header_names:
        if name not in replaced_names:
            merged.append(ucode_lines_by_name[name])
    return "\n".join(merged)


def _required_custom_headers(value: object) -> dict[str, str] | None:
    if not isinstance(value, str):
        return None
    headers: dict[str, str] = {}
    for line in value.splitlines():
        if not line.strip():
            continue
        name, separator, header_value = line.partition(":")
        if (
            not separator
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
            or name.lower() in headers
            or any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in header_value)
        ):
            return None
        headers[name.lower()] = header_value.strip()
    return {
        name: header_value
        for name, header_value in headers.items()
        if name not in CLAUDE_OPTIONAL_ATTRIBUTION_HEADER_NAMES
    }


def _managed_settings_conflicts(
    existing: dict, desired: dict, owned_paths: list[list[str]]
) -> list[str]:
    conflicts = managed_file_conflicts(existing, desired, owned_paths)
    header_path = f"env.{ANTHROPIC_CUSTOM_HEADERS_ENV_KEY}"
    if header_path in conflicts:
        existing_headers = _required_custom_headers(
            (existing.get("env") or {}).get(ANTHROPIC_CUSTOM_HEADERS_ENV_KEY)
        )
        desired_headers = _required_custom_headers(
            (desired.get("env") or {}).get(ANTHROPIC_CUSTOM_HEADERS_ENV_KEY)
        )
        if existing_headers is not None and existing_headers == desired_headers:
            conflicts.remove(header_path)
    return conflicts


def _ug_registered_mcp_names(
    state: dict, managed_servers: object, *, web_search: bool, also: Iterable[str] = ()
) -> set[str]:
    """Every MCP server name ug registers in Claude: its managed-file entries, its user-scope
    fallback registrations, and its own web search server."""
    names = set(managed_servers) if isinstance(managed_servers, dict) else set()
    names.update(also)
    names.update(
        server["name"]
        for server in state.get("managed_mcp_servers") or []
        if isinstance(server, dict)
        and isinstance(server.get("name"), str)
        and "claude" in (server.get("clients") or [])
    )
    if web_search:
        names.add(WEB_SEARCH_MCP_NAME)
    return names


def _delivered_admin_allowlist() -> list | None:
    """The ``allowedMcpServers`` list ug last delivered from the admin's agent_native_settings, if any."""
    leaves = managed_file_snapshots("claude", _parse_managed_settings).settings_passthrough or []
    for leaf in leaves:
        if (
            isinstance(leaf, list)
            and len(leaf) == 2
            and leaf[0] == [CLAUDE_ALLOWED_MCP_SERVERS_KEY]
            and isinstance(leaf[1], list)
        ):
            return leaf[1]
    return None


def _allow_ug_mcp_servers(settings: dict, admin_allowlist: list | None, names: set[str]) -> dict:
    """Admit ug's own MCP servers in an admin's ``allowedMcpServers``.

    Claude Code blocks any server missing from that allowlist and matches it by registered name,
    which admins can't be expected to know, so ug appends its own servers after the admin's entries.
    Recomputed from the admin's list each time, so a server ug stops registering drops out. An
    allowlist that didn't come from agent_native_settings is left alone.
    """
    if admin_allowlist is None:
        return settings
    listed = {entry.get("serverName") for entry in admin_allowlist if isinstance(entry, dict)}
    settings[CLAUDE_ALLOWED_MCP_SERVERS_KEY] = [
        *admin_allowlist,
        *({"serverName": name} for name in sorted(names - listed)),
    ]
    return settings


def _is_shared_claude_list(path: list[str]) -> bool:
    """Lists IT and ug also write to in the managed file, so the admin's entries merge into them."""
    return path == CLAUDE_PERMISSIONS_DENY_PATH or (len(path) == 2 and path[0] == "hooks")


def _warn_if_settings_disable_smart_routing(
    passthrough: SettingsPassthrough, previous_leaves: list
) -> None:
    if passthrough.record() == previous_leaves:
        return
    lockdown = [
        ".".join(path)
        for path, value in passthrough.leaves
        if value is True and path in ([key] for key in CLAUDE_HOOK_LOCKDOWN_KEYS)
    ]
    if lockdown:
        print_warning(
            f"Your managed settings set {', '.join(lockdown)}, which also blocks the per-launch "
            "hooks ug's smart routing installs; smart-routed launches will run unrouted."
        )


def _warn_unrecorded_allow_list(merged: dict, snapshots: ManagedFileSnapshots) -> None:
    """Flag an enforced model allow-list ug may have written before it recorded its pickers.

    Without a record ug can't tell it from an administrator's, so it is never removed; say how to
    clear it instead of leaving the old model restriction in place silently."""
    last_applied = snapshots.last_applied_by_ug or {}
    keys = [k for k in ("availableModels", "enforceAvailableModels") if merged.get(k) is not None]
    if keys and all(merged[k] == last_applied.get(k) for k in keys):
        print_warning(
            f"Claude Code managed settings at {_managed_settings_path()} still set "
            f"{', '.join(keys)}, possibly from an earlier ug version. If you didn't expect a model "
            "allow-list, run `ug revert` or ask your administrator to remove it."
        )


def _reconcile_managed_settings(
    state: dict,
    compose: Callable[[dict], dict],
    owned_paths: list[list[str]],
    relayed: bool,
    picker_keys: list[str],
    withdrawn_denies: list | None = None,
    passthrough_leaves: list | None = None,
) -> None:
    """Reconcile Claude Code's OS-managed settings so a bare ``claude`` uses the gateway.

    The managed file is root-owned and the highest-precedence scope, so every normal Claude
    configuration mirrors ucode's settings there. The same compose operation that produced the
    private file is applied to the existing managed file, preserving unrelated IT-authored keys.

    `ug configure` updates gateway-owned fields in this file. It writes the picker
    (`availableModels`/`modelPicker`) for a static managed list and removes the picker keys it
    previously wrote when it no longer manages one, leaving an administrator's own picker untouched.

    Relayed launches are skipped: they depend on a per-session loopback refresh proxy that only runs
    during `ucode claude`, so a bare `claude` could not reach the gateway anyway.
    """
    path = _managed_settings_path()
    if path is None:
        print_warning(
            "Machine-wide Claude settings aren't supported on this platform; skipped the managed "
            "settings."
        )
        return
    if path.is_symlink():
        raise RuntimeError(
            f"Refusing to use Claude Code managed settings through symlink {path}. Replace it "
            "with a regular file or contact your administrator."
        )
    if relayed:
        conflicts = _managed_relayed_conflicts(path)
        if conflicts:
            raise RuntimeError(
                "Claude subscription relay cannot start because enterprise managed settings "
                f"define {', '.join(conflicts)} at {path}. Ask your administrator to remove "
                "those entries or use standard Databricks authentication. If ucode previously "
                "created them, run `ucode revert` from an interactive terminal first."
            )
        mark_managed_file_verified(state, "claude", path, scope="relay-compatible")
        return

    current_text = read_managed_file(path)
    try:
        existing = _parse_managed_settings(current_text) if current_text is not None else {}
    except RuntimeError as exc:
        raise RuntimeError(
            f"Cannot safely update Claude Code managed settings at {path}: {exc}. "
            "ucode did not modify the file. Repair it or contact your administrator."
        ) from exc
    managed_before = copy.deepcopy(existing)
    desired_settings = compose(existing)
    _preserve_permission_denies(managed_before, desired_settings, withdrawn=withdrawn_denies or [])
    conflicts = _managed_settings_conflicts(managed_before, desired_settings, owned_paths)
    if not managed_writes_allowed() and not conflicts:
        mark_managed_file_verified(state, "claude", path, scope="local-compatible")
        return
    try:
        reconcile_managed_file(
            path,
            _dump_managed_settings(desired_settings),
            tool="claude",
            display="Claude Code",
            owned_paths=owned_paths,
            parser=_parse_managed_settings,
        )
    except ManagedFileWriteUnavailable:
        if conflicts:
            raise
        print_warning(
            f"Claude Code OS-managed settings could not be updated at {path}; continuing with "
            f"local settings at {CLAUDE_SETTINGS_PATH}."
        )
        mark_managed_file_verified(state, "claude", path, scope="local-compatible")
        return
    mark_managed_file_verified(state, "claude", path)
    record_ug_picker("claude", {key: desired_settings[key] for key in picker_keys})
    record_settings_passthrough("claude", passthrough_leaves or [])


def _preserve_permission_denies(
    existing: dict, desired: dict, *, withdrawn: Sequence[object] = ()
) -> None:
    """Keep the deny rules already in ``existing`` alongside ``desired``'s, except ``withdrawn``.

    ``withdrawn`` holds rules ug delivered from the admin's settings that the admin has since dropped;
    every other existing rule (IT-authored ones included) stays.
    """
    existing_permissions = existing.get("permissions")
    desired_permissions = desired.get("permissions")
    if not isinstance(existing_permissions, dict) or not isinstance(desired_permissions, dict):
        return
    existing_denies = existing_permissions.get("deny")
    desired_denies = desired_permissions.get("deny")
    if not isinstance(existing_denies, list) or not isinstance(desired_denies, list):
        return
    kept = [rule for rule in existing_denies if rule not in withdrawn]
    desired_permissions["deny"] = [*kept, *(rule for rule in desired_denies if rule not in kept)]


def default_model(state: dict) -> str | None:
    claude_models = state.get("claude_models") or {}
    return (
        claude_models.get("opus")
        or claude_models.get("sonnet")
        or claude_models.get("haiku")
        or next(iter(claude_models.values()), None)
    )


def _extract_caller_settings(tool_args: list[str]) -> tuple[list[str], list[str]]:
    """Split caller-supplied ``--settings`` values out of *tool_args*.

    Returns ``(values, remaining_args)``, handling both ``--settings <value>``
    and ``--settings=<value>`` spellings. Each value is either a JSON string or
    a path to a settings file — Claude Code accepts either.
    """
    values: list[str] = []
    remaining: list[str] = []
    i = 0
    while i < len(tool_args):
        arg = tool_args[i]
        if arg == "--settings" and i + 1 < len(tool_args):
            values.append(tool_args[i + 1])
            i += 2
            continue
        if arg.startswith("--settings="):
            values.append(arg[len("--settings=") :])
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return values, remaining


def _load_caller_settings(value: str) -> dict:
    """Resolve a ``--settings`` value (inline JSON or file path) to a dict.

    Claude Code accepts either inline JSON or a path to a JSON file. Raises
    ``RuntimeError`` (surfaced by the CLI as an actionable error) when the value
    is neither, rather than silently dropping it: a dropped value would also be
    passed through as a second ``--settings`` flag, and Claude Code honors only
    one — so either the caller's settings or ucode's gateway config would be
    silently ignored. Failing loudly lets the caller fix their input.
    """
    text = value.strip()
    if text.startswith("{"):
        source, malformed = text, "value is not valid JSON"
    else:
        path = Path(text)
        if not path.exists():
            raise RuntimeError(
                f"--settings file not found: {value!r}. "
                "Pass inline JSON or a path to an existing JSON file."
            )
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"--settings file could not be read: {value!r} ({exc}).") from exc
        malformed = "file is not valid JSON"
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"--settings {malformed} ({exc}): {value!r}. Pass inline JSON or a path to a JSON file."
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"--settings must be a JSON object, got {type(parsed).__name__}: {value!r}."
        )
    return parsed


def _union_claude_hooks(base: dict, overlay: dict) -> dict:
    """Union two Claude Code ``hooks`` maps.

    ``hooks`` is ``{event: [entry, ...]}``. Per event we concatenate the entry
    lists so hooks from BOTH settings sources fire, rather than one replacing
    the other (which is what a plain deep-merge does to lists). This is what
    lets ucode's tracing Stop hook and a caller's own hooks coexist.
    """
    result: dict = {}
    for event in [*base, *(e for e in overlay if e not in base)]:
        entries: list = []
        for src in (base, overlay):
            val = src.get(event)
            if isinstance(val, list):
                entries.extend(val)
        result[event] = entries
    return result


def _merge_claude_settings(base: dict, overlay: dict) -> dict:
    """Deep-merge *overlay* onto *base* (overlay wins on conflicting leaves),
    preserving both sources' hooks and permission denies. Inputs are not mutated.
    """
    merged = deep_merge_dict(copy.deepcopy(base), overlay)
    _preserve_permission_denies(base, merged)
    base_hooks = base.get("hooks")
    overlay_hooks = overlay.get("hooks")
    if isinstance(base_hooks, dict) or isinstance(overlay_hooks, dict):
        merged["hooks"] = _union_claude_hooks(
            base_hooks if isinstance(base_hooks, dict) else {},
            overlay_hooks if isinstance(overlay_hooks, dict) else {},
        )
    return merged


def _compose_v2_settings(tool_args: list[str]) -> tuple[dict, list[str]]:
    """Compose caller settings with ucode's Claude settings for a v2 launch."""
    caller_values, remaining = _extract_caller_settings(tool_args)
    settings: dict = {}
    for value in caller_values:
        settings = _merge_claude_settings(settings, _load_caller_settings(value))
    return _merge_claude_settings(settings, read_json_safe(CLAUDE_SETTINGS_PATH)), remaining


def _launch_model_args(tool_args: list[str], launch_model: str | None) -> list[str]:
    if not launch_model or has_explicit_model_arg(tool_args):
        return []
    return ["--model", launch_model]


def _resolve_picker_model_id(model: str, settings_env: dict) -> str:
    """Resolve configured aliases and context suffixes for comparisons only."""
    model = re.sub(r"\[(?:1m|200k)\]$", "", model)
    family_env_key = CLAUDE_DEFAULT_MODEL_ENV_KEYS.get(model)
    if family_env_key:
        family_model = settings_env.get(family_env_key)
        if isinstance(family_model, str) and family_model:
            model = family_model
    return re.sub(r"\[(?:1m|200k)\]$", "", model)


def _resolve_launch_binary(binary: str) -> str:
    """Resolve Claude's native executable without sending arguments through a batch shim."""
    if os.name != "nt":
        return binary

    resolved = shutil.which(binary)
    if resolved is None:
        raise RuntimeError(
            "Claude Code was not found on PATH. Install Claude Code and ensure its executable "
            "is available, then retry."
        )
    if os.path.splitext(resolved)[1].casefold() not in {".bat", ".cmd"}:
        return resolved

    shim_dir = os.path.dirname(resolved)
    node_modules_dirs: list[str] = []
    if (
        os.path.basename(shim_dir).casefold() == ".bin"
        and os.path.basename(os.path.dirname(shim_dir)).casefold() == "node_modules"
    ):
        node_modules_dirs.append(os.path.dirname(shim_dir))
    node_modules_dirs.append(os.path.join(shim_dir, "node_modules"))

    for node_modules in node_modules_dirs:
        native_binary = os.path.join(
            node_modules,
            "@anthropic-ai",
            "claude-code",
            "bin",
            "claude.exe",
        )
        if os.path.isfile(native_binary):
            return native_binary

    raise RuntimeError(
        f"Found the Claude Code Windows command shim at {resolved}, but its native "
        "bin/claude.exe was missing. Upgrade or reinstall @anthropic-ai/claude-code and retry."
    )


def _build_claude_argv(
    binary: str,
    tool_args: list[str],
    relayed: bool = False,
    settings_override: dict | None = None,
) -> list[str]:
    """Build the ``claude`` argv, composing any caller ``--settings`` with
    ucode's managed settings.

    ucode needs its own settings (gateway ``apiKeyHelper`` + env) to reach
    Claude, and normally passes ``--settings <ucode-file>``. But Claude Code
    honors only ONE ``--settings`` flag, so a caller that ALSO passes
    ``--settings`` (e.g. an integration injecting hooks) would have exactly one
    of the two silently dropped. To let ucode compose with any prior command,
    we merge a caller-supplied ``--settings`` with ucode's — ucode's gateway
    keys win, hooks and denies from both are unioned — and hand Claude a single merged
    ``--settings`` (inline JSON). The merge is per-launch and is never written
    back to the shared ucode settings file, so concurrent launches cannot
    accumulate one another's hooks. A caller ``--settings`` value ucode cannot
    resolve raises (see :func:`_load_caller_settings`) rather than being passed
    through as a second, colliding flag.

    ``relayed`` adds ``--setting-sources`` to exclude the user scope (see
    :data:`_RELAYED_SETTING_SOURCES`), so a stale user-scope apiKeyHelper cannot
    filter through and shadow the subscription OAuth.
    """
    source_args = ["--setting-sources", _RELAYED_SETTING_SOURCES] if relayed else []
    caller_values, remaining = _extract_caller_settings(tool_args)
    if not caller_values and settings_override is None:
        # No caller --settings: hand Claude ucode's settings file directly (the
        # common path; behavior unchanged).
        return [binary, *source_args, "--settings", str(CLAUDE_SETTINGS_PATH), *tool_args]
    caller_settings: dict = {}
    for value in caller_values:
        caller_settings = _merge_claude_settings(caller_settings, _load_caller_settings(value))
    # ucode wins over the caller for conflicting keys (protects gateway auth);
    # hooks and permission denies from both sides survive.
    merged = _merge_claude_settings(caller_settings, read_json_safe(CLAUDE_SETTINGS_PATH))
    if settings_override is not None:
        merged = _merge_claude_settings(merged, settings_override)
    merged_env = merged.get("env")
    if isinstance(merged_env, dict):
        merged_env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
    return [
        binary,
        *source_args,
        "--settings",
        json.dumps(merged, separators=(",", ":")),
        *remaining,
    ]


def _has_subscription_login() -> bool:
    """True when Claude Code already holds a subscription login (`claude auth
    status` exits 0). Never inspects or captures the credential itself."""
    try:
        result = subprocess_cross_os.run(
            [SPEC["binary"], "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _ensure_subscription_login() -> None:
    """Ensure Claude Code has a persisted subscription login, running the browser
    flow via `claude auth login` if not. ucode never sees or stores the token —
    Claude Code persists it to its own secure store and refreshes it natively."""
    # The OAuth token is the Authorization credential directly, so no interactive login
    # applies — return early so unattended runs can't hang on the browser fallback.
    is_headless_mode = os.environ.get(CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR)
    if is_headless_mode:
        return
    if _has_subscription_login():
        return
    print_note("Opening browser to sign in with your Claude subscription...")
    try:
        subprocess_cross_os.run([SPEC["binary"], "auth", "login"], check=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("`claude auth login` failed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`claude auth login` timed out.") from exc
    print_success("Claude subscription authenticated")


def _rewrite_relayed_port(state: dict, port: int) -> None:
    """Point the persisted config + state at ``port`` after the proxy had to bind
    a different port than the cached one. Keeps ANTHROPIC_BASE_URL (which Claude
    Code reads) in sync with the live proxy so requests reach it."""
    state["relayed_proxy_port"] = port
    save_state(state)
    settings = read_json_safe(CLAUDE_SETTINGS_PATH)
    env = settings.get("env")
    if isinstance(env, dict):
        env["ANTHROPIC_BASE_URL"] = f"http://{LOOPBACK_HOST}:{port}"
        write_json_file(CLAUDE_SETTINGS_PATH, settings)


def _launch_relayed(state: dict, binary: str, tool_args: list[str]) -> None:
    """Relayed launch: sign into the Claude subscription, start the loopback
    refresh proxy, then run Claude Code alongside it (the proxy must outlive the
    exec, so we spawn-and-wait rather than replacing the process)."""
    _ensure_subscription_login()
    workspace = state["workspace"]
    port = state.get("relayed_proxy_port")
    if not isinstance(port, int):
        raise RuntimeError("Relayed proxy port was not configured; re-run `ucode claude`.")

    profile = state.get("profile")

    def token_provider(force_refresh: bool) -> str:
        return get_databricks_token(workspace, profile, force_refresh=force_refresh)

    server, cache, client = gateway_proxy.start_relay_proxy(workspace, token_provider, port)
    # start_relay_proxy falls back to an OS-assigned port when the cached one is taken
    # (stale proxy from a killed session). Reconcile settings + state to whatever
    # it actually bound, so Claude Code connects to the live port.
    bound_port = server.server_address[1]
    if bound_port != port:
        _rewrite_relayed_port(state, bound_port)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    proc = subprocess_cross_os.popen(_build_claude_argv(binary, tool_args, relayed=True))
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    finally:
        cache.stop()
        server.shutdown()
        client.close()
    raise SystemExit(returncode)


def launch(
    state: dict,
    tool_args: list[str],
    *,
    options: LaunchOptions,
) -> None:
    tool_args = _external_web_search_args(state, tool_args)
    binary = SPEC["binary"]
    workspace = state.get("workspace")
    if workspace and os.environ.get(GATEWAY_MODEL_DISCOVERY_ENV_VAR) == "1":
        # Discovery is launch-scoped. Pass it in the process environment rather
        # than persisting it in Claude's private or OS-managed settings.
        os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    if state.get("claude_relayed"):
        _launch_relayed(state, binary, tool_args)
        return
    launch_default_model = state.get("_claude_launch_default_model")
    if isinstance(launch_default_model, str) and launch_default_model:
        os.environ["ANTHROPIC_DEFAULT_MODEL"] = launch_default_model
    # Smart routing also spawns Claude directly, so it needs the native executable.
    binary = _resolve_launch_binary(binary)
    routing_setup_failed = False
    if options.launch_smart_routing:
        try:
            smart_routing_v2.launch_claude(
                state,
                tool_args,
                binary=binary,
                user_settings_path=CLAUDE_USER_SETTINGS_PATH,
                # With no user pin, let Claude resolve its starting model from its own settings.
                launch_model=options.user_pinned_model,
                compose_settings=_compose_v2_settings,
                launch_model_args=_launch_model_args,
                model_name=_maybe_add_1m_suffix,
            )
        except smart_routing_v2.ClaudeRoutingSetupError:
            _debug("Claude smart-routing setup failed; launching normally", traceback.format_exc())
            routing_setup_failed = True
        else:
            return
    if workspace and not custom_oauth_cli_enabled(state.get("custom_oauth")):
        os.environ["OAUTH_TOKEN"] = get_databricks_token(workspace, state.get("profile"))
    settings_override = None
    launch_args = list(tool_args)
    if options.user_pinned_model:
        # Pass the exact ID: managed family defaults can override temporary aliases.
        os.environ["ANTHROPIC_MODEL"] = options.user_pinned_model
        settings_override = {"env": {"ANTHROPIC_MODEL": options.user_pinned_model}}
        launch_args = [
            *_launch_model_args(tool_args, options.user_pinned_model),
            *tool_args,
        ]
    else:
        picker_models = state.get("_claude_launch_picker_models")
        if isinstance(picker_models, list) and picker_models:
            saved_model = read_json_safe(CLAUDE_USER_SETTINGS_PATH).get("model")
            settings_env = read_json_safe(CLAUDE_SETTINGS_PATH).get("env")
            settings_env = settings_env if isinstance(settings_env, dict) else {}
            available_models = {
                _resolve_picker_model_id(model, settings_env) for model in picker_models
            }
            if (
                not isinstance(saved_model, str)
                or _resolve_picker_model_id(saved_model, settings_env) not in available_models
            ):
                # Launch on a valid discovered model without turning it into a managed default or
                # overwriting the user's saved selection. This also prevents Claude from appending
                # that stale built-in selection to an otherwise replaced picker.
                settings_override = {"model": picker_models[0]}
    if routing_setup_failed:
        # Override inherited and saved routing flags for this launch only. Older
        # saved hooks must not route to agents whose plugin could not be written.
        fallback_env = {
            smart_routing_v2.ENABLE_SMART_ROUTING_ENV_VAR: "0",
            smart_routing_v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "0",
            FIRST_PROMPT_SOCKET_ENV: "",
        }
        settings_override = _merge_claude_settings(settings_override or {}, {"env": fallback_env})
        os.environ.update(fallback_env)
    exec_or_spawn(_build_claude_argv(binary, launch_args, settings_override=settings_override))


def validate_cmd(binary: str) -> list[str]:
    return [
        binary,
        "--settings",
        str(CLAUDE_SETTINGS_PATH),
        "-p",
        "say hi in 5 words or less",
        "--max-turns",
        "1",
    ]
