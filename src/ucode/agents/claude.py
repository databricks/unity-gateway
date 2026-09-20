"""Claude Code agent: writes ~/.claude/settings.json env block."""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ucode import gateway_proxy
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    atomic_write_json,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from ucode.constants import (
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
    get_custom_client_token,
)
from ucode.databricks import (
    AnthropicModelCatalog,
    build_auth_shell_command,
    build_otel_headers_shell_command,
    build_otel_traces_endpoint,
    build_tool_base_url,
    fetch_anthropic_gateway_models,
    get_databricks_token,
    ug_binary,
)
from ucode.launcher import exec_or_spawn
from ucode.managed_files import (
    OS,
    ManagedFileSnapshots,
    ManagedFileWriteUnavailable,
    current_os,
    managed_file_conflicts,
    managed_file_is_verified,
    managed_file_scope,
    managed_file_snapshots,
    managed_file_status,
    managed_files_supported,
    managed_writes_allowed,
    mark_managed_file_verified,
    read_managed_file,
    reconcile_managed_file,
    revert_managed_file,
)
from ucode.mcp_oauth import (
    CLAUDE_CODE_OAUTH_CLIENT_ID,
    MCP_OAUTH_CALLBACK_PORT,
    oauth_client_available,
)
from ucode.smart_routing import v2 as smart_routing_v2
from ucode.smart_routing.claude_hooks import (
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
MINIMUM_CLAUDE_VERSION = (2, 1, 259)
MINIMUM_CLAUDE_VERSION_TEXT = "2.1.259"
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
# Relayed drops the user scope to deliberately omit the stale apiKeyHelper. Only applied to relayed
# launches — normal launches keep loading user settings (hooks/permissions) as before.
_RELAYED_SETTING_SOURCES = "project,local"


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


def _dump_managed_settings(settings: dict) -> str:
    return json.dumps(settings, indent=2) + "\n"


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


def _web_search_mcp_entry(workspace: str, search_model: str, profile: str | None = None) -> dict:
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
        "args": ["mcp", "web-search"],
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
    if smart_routing_v2.smart_routing_enabled():
        header_lines.append(f"{SMART_ROUTER_RECIPE_HEADER}: {configured_router_name()}")
    # Relayed: the X-Databricks-AI-Gateway-Token swap header is added per request
    # by the refresh proxy, not here — a static value would go stale mid-session.
    custom_headers = "\n".join(header_lines)
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
        subprocess.run(
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
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Failed to add HTTP MCP server '{name}' via claude CLI.") from exc


def remove_claude_mcp_server(name: str, scope: str) -> bool:
    # Imported lazily: `_is_missing_mcp_server_output` is a shared CLI-output matcher
    # in ucode.mcp (used by the codex/gemini removers too), and ucode.mcp imports
    # this module at load time — a function-level import avoids that cycle.
    from ucode.mcp import _is_missing_mcp_server_output

    try:
        subprocess.run(
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


def reconcile_managed_mcp(state: dict, servers: dict[str, dict]) -> bool:
    """Overwrite ug's ``managedMcpServers`` in Claude's OS-managed file with ``servers``.

    ``servers`` is the freshly resolved managed set keyed by name; an empty map clears the key. The
    managed file is the source of truth, so this is a wipe-and-rewrite, not a diff. Every other
    managed key is preserved, including the model configuration ug wrote earlier this run and any
    admin-authored policy. Returns True when the managed file is the delivery mechanism (written or
    already current), False when it cannot be used (unsupported platform or a non-interactive run),
    so the caller routes those servers to the user-scope registration instead."""
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
    # Nothing managed to clear: never create or rewrite the file just to remove an absent key.
    if not servers and MANAGED_MCP_SETTINGS_KEY not in existing:
        return True
    desired = copy.deepcopy(existing)
    if servers:
        desired[MANAGED_MCP_SETTINGS_KEY] = servers
    else:
        desired.pop(MANAGED_MCP_SETTINGS_KEY, None)
    try:
        reconcile_managed_file(
            path,
            _dump_managed_settings(desired),
            tool="claude",
            display="Claude Code",
            owned_paths=[[MANAGED_MCP_SETTINGS_KEY]],
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


def _register_web_search_mcp(workspace: str, search_model: str, profile: str | None = None) -> bool:
    """Register (or replace) the web_search MCP server in Claude Code's user
    scope via `claude mcp add-json`. Removes any prior entry first so re-runs
    pick up changes to the workspace, model, or ucode binary path.

    Returns True if registration succeeded. Failures are non-blocking: we warn
    and return False so the rest of `ucode claude` setup can complete.
    """
    for scope in MCP_CLEANUP_SCOPES:
        try:
            remove_claude_mcp_server(WEB_SEARCH_MCP_NAME, scope)
        except RuntimeError:
            # Best-effort cleanup of stale entries — keep going.
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
    config = read_json_safe(CLAUDE_MCP_CONFIG_PATH)
    servers = config.get("mcpServers")
    return isinstance(servers, dict) and servers.get(WEB_SEARCH_MCP_NAME) == entry


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
    # Back up only a file that predates ucode's management of the tool. A
    # re-configure would otherwise snapshot ucode's own generated file, and
    # revert would restore that snapshot instead of deleting the file.
    if not is_tool_managed(state, "claude"):
        backup_existing_file(CLAUDE_SETTINGS_PATH, CLAUDE_BACKUP_PATH)
    previous_keys = ((state.get("managed_configs") or {}).get("claude") or {}).get("keys", [])
    web_search_model = _resolve_web_search_model(state)
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
        otel_tracing=bool(state.get("claude_otel_tracing")),
        picker_catalog=picker_catalog,
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
        merged = deep_merge_dict(base, overlay_for_merge)
        for key in stale_picker_keys:
            merged.pop(key, None)
        overlay_custom_headers = overlay_for_merge["env"][ANTHROPIC_CUSTOM_HEADERS_ENV_KEY]
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
            for key in CLAUDE_OTEL_TRACE_ENV_KEYS:
                if key not in overlay_env:
                    merged_env.pop(key, None)
            # deep_merge_dict keeps keys already in the file, so drop the ones ucode no
            # longer writes.
            for key in CLAUDE_REMOVED_ENV_KEYS:
                merged_env.pop(key, None)
        if not any(key in overlay_for_merge for key in CLAUDE_MANAGED_PICKER_KEYS):
            if managed_settings_snapshots is None:
                for key in CLAUDE_MANAGED_PICKER_KEYS:
                    merged.pop(key, None)
            elif managed_settings_snapshots.last_applied_by_ug is not None:
                last_applied = managed_settings_snapshots.last_applied_by_ug
                live_picker = [merged.get(key) for key in CLAUDE_MANAGED_PICKER_KEYS]
                ucode_picker = [last_applied.get(key) for key in CLAUDE_MANAGED_PICKER_KEYS]
                if live_picker == ucode_picker:
                    baseline = managed_settings_snapshots.original_before_ug or {}
                    for key in CLAUDE_MANAGED_PICKER_KEYS:
                        if key in baseline:
                            merged[key] = baseline[key]
                        else:
                            merged.pop(key, None)
        if "otelHeadersHelper" not in overlay_for_merge:
            merged.pop("otelHeadersHelper", None)
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

    _reconcile_managed_settings(
        state,
        lambda base: _compose(
            base,
            enforce_model_default_hierarchy=(
                source_scoped_defaults or (provider is None and parent_schema is None)
            ),
            managed_settings_snapshots=managed_snapshots,
        ),
        managed_file_keys,
        relayed,
    )

    if web_search_model:
        web_search_entry = _web_search_mcp_entry(
            state["workspace"], web_search_model, state.get("profile")
        )
        if not _web_search_mcp_is_current(state, web_search_entry):
            # Registration runs multiple `claude mcp` subprocesses and can take several seconds.
            registration_success = _register_web_search_mcp(
                state["workspace"], web_search_model, state.get("profile")
            )
            if registration_success:
                state[WEB_SEARCH_MCP_STATE_KEY] = web_search_entry
    else:
        state.pop(WEB_SEARCH_MCP_STATE_KEY, None)

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


def _reconcile_managed_settings(
    state: dict,
    compose: Callable[[dict], dict],
    owned_paths: list[list[str]],
    relayed: bool,
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
    _preserve_permission_denies(managed_before, desired_settings)
    if not managed_writes_allowed():
        conflicts = managed_file_conflicts(managed_before, desired_settings, owned_paths)
        if conflicts:
            raise RuntimeError(
                "Claude Code configuration cannot be applied non-interactively because "
                f"OS-managed settings at {path} override ucode values: {', '.join(conflicts)}. "
                "Run `ucode configure --agent claude` from an interactive terminal or contact "
                "your administrator."
            )
        mark_managed_file_verified(state, "claude", path, scope="local-compatible")
        return
    try:
        reconcile_managed_file(
            path,
            _dump_managed_settings(desired_settings),
            tool="claude",
            display="Claude Code",
            owned_paths=owned_paths,
        )
    except ManagedFileWriteUnavailable:
        conflicts = managed_file_conflicts(managed_before, desired_settings, owned_paths)
        if conflicts:
            raise
        print_warning(
            f"Claude Code OS-managed settings could not be updated at {path}; continuing with "
            f"local settings at {CLAUDE_SETTINGS_PATH}."
        )
        mark_managed_file_verified(state, "claude", path, scope="local-compatible")
        return
    mark_managed_file_verified(state, "claude", path)


def _preserve_permission_denies(existing: dict, desired: dict) -> None:
    existing_permissions = existing.get("permissions")
    desired_permissions = desired.get("permissions")
    if not isinstance(existing_permissions, dict) or not isinstance(desired_permissions, dict):
        return
    existing_denies = existing_permissions.get("deny")
    desired_denies = desired_permissions.get("deny")
    if not isinstance(existing_denies, list) or not isinstance(desired_denies, list):
        return
    desired_permissions["deny"] = [
        *existing_denies,
        *(rule for rule in desired_denies if rule not in existing_denies),
    ]


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
    but UNION the ``hooks`` so neither side's hooks are dropped. Inputs are not
    mutated.
    """
    merged = deep_merge_dict(copy.deepcopy(base), overlay)
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
    keys win, hooks from both are unioned — and hand Claude a single merged
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
    # hooks from both sides survive.
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
        result = subprocess.run(
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
        subprocess.run([SPEC["binary"], "auth", "login"], check=True, timeout=300)
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


def _refresh_gateway_models_cache(state: dict) -> None:
    if os.environ.get(GATEWAY_MODEL_DISCOVERY_ENV_VAR) != "1" or not state.get("workspace"):
        return
    workspace = state["workspace"]
    custom_oauth = state.get("custom_oauth")
    token = (
        get_custom_client_token(workspace, **custom_oauth)
        if custom_oauth
        else get_databricks_token(workspace, state.get("profile"))
    )
    env = read_json_safe(CLAUDE_SETTINGS_PATH).get("env", {})
    headers = {}
    for line in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines():
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip()] = value.strip()
    models, reason = fetch_anthropic_gateway_models(workspace, token, headers=headers)
    if models is None:
        raise RuntimeError(
            f"Could not refresh Claude models: {reason}. Check your model source and retry."
        )
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or CLAUDE_CONFIG_DIR)
    atomic_write_json(
        config_dir / "cache" / "gateway-models.json",
        {
            "baseUrl": env.get("ANTHROPIC_BASE_URL") or build_tool_base_url("claude", workspace),
            "fetchedAt": int(time.time() * 1000),
            "models": models,
        },
    )


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

    try:
        _refresh_gateway_models_cache(state)
        proc = subprocess.Popen(_build_claude_argv(binary, tool_args, relayed=True))
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
    binary = SPEC["binary"]
    workspace = state.get("workspace")
    if workspace and os.environ.get(GATEWAY_MODEL_DISCOVERY_ENV_VAR) == "1":
        # Discovery is launch-scoped. Pass it in the process environment rather
        # than persisting it in Claude's private or OS-managed settings.
        os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    if state.get("claude_relayed"):
        _launch_relayed(state, binary, tool_args)
        return
    _refresh_gateway_models_cache(state)
    # Smart routing needs Unix PTY support, which Windows does not provide.
    if options.launch_smart_routing and os.name == "nt":
        raise RuntimeError(
            "Smart routing in Claude Code is currently not supported on Windows. "
            "Please use Codex or disable smart routing."
        )
    if options.launch_smart_routing:
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
        return
    if workspace and not custom_oauth_cli_enabled(state.get("custom_oauth")):
        os.environ["OAUTH_TOKEN"] = get_databricks_token(workspace, state.get("profile"))
    settings_override = None
    launch_args = list(tool_args)
    if options.user_pinned_model:
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
            if saved_model not in picker_models:
                # Launch on a valid discovered model without turning it into a managed default or
                # overwriting the user's saved selection. This also prevents Claude from appending
                # that stale built-in selection to an otherwise replaced picker.
                settings_override = {"model": picker_models[0]}
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
