"""OpenCode agent: writes opencode.json with Databricks-backed providers."""

from __future__ import annotations

import os
import signal

from ucode import metrics
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    apply_json_mcp_diff,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
    write_text_file,
)
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER
from ucode.databricks import (
    build_opencode_base_urls,
    get_databricks_token,
    model_token_limits,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_user_agent, ug_version
from ucode.workspace import workspace_headers

from . import opencode_family
from .args import LaunchOptions, explicit_model_arg_value, has_explicit_model_arg
from .request_tags import request_tags_header_value

OPENCODE_XDG_CONFIG_HOME = APP_DIR / "opencode-xdg"
OPENCODE_CONFIG_DIR = OPENCODE_XDG_CONFIG_HOME / "opencode"
OPENCODE_CONFIG_PATH = OPENCODE_CONFIG_DIR / "opencode.json"
OPENCODE_BACKUP_PATH = APP_DIR / "opencode-config.backup.json"
OPENCODE_AUTH_PLUGIN_PATH = OPENCODE_CONFIG_DIR / "plugin" / "ucode-auth.js"
OPENCODE_NPM_PACKAGE = "opencode-ai"
MINIMUM_OPENCODE_VERSION = (1, 0, 220)
MINIMUM_OPENCODE_VERSION_TEXT = "1.0.220"

SPEC: ToolSpec = {
    "binary": "opencode",
    # npm's `latest` tag currently points at an old 0.0.0 beta. Stay on the
    # stable v1 line for both fresh installs and updates.
    "package": f"{OPENCODE_NPM_PACKAGE}@1",
    "display": "OpenCode",
    "config_path": OPENCODE_CONFIG_PATH,
    "backup_path": OPENCODE_BACKUP_PATH,
}

PROVIDER_KEYS: list[list[str]] = opencode_family.provider_keys()


_SETTINGS = opencode_family.FamilySettings(
    display="OpenCode",
    default_model_state_key="opencode_default_model",
    empty_models_hint=None,
)

_parse_version = opencode_family.parse_version


def _minimum_version_message() -> str | None:
    return opencode_family.minimum_version_message(
        _SETTINGS,
        agent_version(SPEC["binary"]),
        MINIMUM_OPENCODE_VERSION,
        MINIMUM_OPENCODE_VERSION_TEXT,
    )


def minimum_version_error() -> str | None:
    return opencode_family.minimum_version_error(_minimum_version_message(), SPEC["package"])


def _uses_v2_plugin_api() -> bool:
    parsed = _parse_version(agent_version(SPEC["binary"]))
    return parsed is not None and parsed[0] >= 2


def render_auth_plugin(state: dict, *, v2: bool = False) -> str:
    """Render the local OpenCode plugin that refreshes Databricks auth on demand."""
    return opencode_family.render_auth_plugin(state, v2=v2)


def write_auth_plugin(state: dict) -> None:
    """Install the auto-discovered hook in ucode's isolated OpenCode config."""
    # Derive the path so tests that redirect OPENCODE_CONFIG_PATH stay isolated.
    # OpenCode 2 still discovers the singular `plugin/` directory.
    path = OPENCODE_CONFIG_PATH.parent / "plugin" / OPENCODE_AUTH_PLUGIN_PATH.name
    write_text_file(path, render_auth_plugin(state, v2=_uses_v2_plugin_api()))


_resolve_model_selector = opencode_family.resolve_model_selector


def resolve_explicit_model(model: str, state: dict) -> str:
    """Resolve and validate a model explicitly selected for an OpenCode launch."""
    return opencode_family.resolve_explicit_model(_SETTINGS, model, state)


def _oss_model_overlay(model: str, ua_header: dict[str, str]) -> dict:
    """Per-model overlay for an OSS model entry.

    All OSS models carry the User-Agent header; models with known token limits
    also pin `limit` (context + output) so OpenCode clamps `max_tokens` to a
    value the gateway accepts. OpenCode's schema requires both fields together,
    so the limits table always supplies both."""
    overlay: dict = {"headers": ua_header}
    limits = model_token_limits(model)
    if limits is not None:
        overlay["limit"] = limits
    return overlay


def render_overlay(
    model: str,
    token: str,
    opencode_base_urls: dict[str, str],
    opencode_models: dict[str, list[str]],
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for opencode.json."""
    auth_headers = {"Authorization": f"Bearer {token}"}
    # OpenCode hardcodes `User-Agent: opencode/<ver>` in session/llm.ts for
    # every provider, after the AI SDK's combineHeaders. The provider-level
    # `headers` are clobbered by that injection, but per-model `headers` are
    # merged AFTER and win — so the UA must live on each model entry.
    # Per-model headers. Like the UA, request tags must ride here (not on the
    # provider `options`), since OpenCode's per-model `headers` win over the
    # provider-level ones after its own UA injection.
    ua_header = {
        "User-Agent": ucode_user_agent(ug_version(), f"opencode/{agent_version('opencode')}"),
    }
    request_tags = request_tags_header_value()
    if request_tags:
        ua_header[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    gateway_url = next(iter(opencode_base_urls.values()), None)
    selector = workspace_headers(gateway_url) if gateway_url else {}
    auth_headers.update(selector)
    ua_header.update(selector)

    anthropic_models = opencode_models.get("anthropic") or []
    gemini_models = opencode_models.get("gemini") or []
    oss_models = opencode_models.get("oss") or []

    providers: dict = {}
    keys: list[list[str]] = [["model"]]
    if anthropic_models:
        # @ai-sdk/anthropic injects `eager_input_streaming: true` on tool defs;
        # the Databricks gateway's strict validator rejects it. opencode's
        # auto-disable in transform.ts skips models whose id contains "claude",
        # so we opt out per-model. The setting lives in per-call providerOptions,
        # which opencode reads from `models.<m>.options`, not provider `options`.
        anthropic_model_overlay = {
            "headers": ua_header,
            "options": {"toolStreaming": False},
        }
        providers["databricks-anthropic"] = {
            "npm": "@ai-sdk/anthropic",
            "options": {
                "baseURL": opencode_base_urls["anthropic"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": dict.fromkeys(anthropic_models, anthropic_model_overlay),
        }
        keys.append(["provider", "databricks-anthropic"])
    if gemini_models:
        providers["databricks-google"] = {
            "npm": "@ai-sdk/google",
            "options": {
                "baseURL": opencode_base_urls["gemini"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": {m: {"headers": ua_header} for m in gemini_models},
        }
        keys.append(["provider", "databricks-google"])
    if oss_models:
        providers["databricks-oss"] = {
            "npm": "@ai-sdk/openai",
            "options": {
                "baseURL": opencode_base_urls["oss"],
                "apiKey": token,
                "headers": auth_headers,
            },
            "models": {m: _oss_model_overlay(m, ua_header) for m in oss_models},
        }
        keys.append(["provider", "databricks-oss"])

    overlay: dict = {"model": _resolve_model_selector(model, opencode_models)}
    if providers:
        overlay["provider"] = providers
    return overlay, keys


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
) -> tuple[dict, str]:
    backup_existing_file(OPENCODE_CONFIG_PATH, OPENCODE_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(state["workspace"], state.get("profile"))
    opencode_base_urls = state.get("base_urls", {}).get("opencode") or build_opencode_base_urls(
        state["workspace"]
    )
    overlay, managed_keys = render_overlay(
        model,
        token,
        opencode_base_urls,
        state.get("opencode_models") or {},
    )
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    write_auth_plugin(state)
    providers = existing.get("provider")
    if isinstance(providers, dict):
        for stale in (
            "databricks-anthropic",
            "databricks-google",
            "databricks-openai",
            "databricks-oss",
        ):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_json_file(OPENCODE_CONFIG_PATH, merged)
    state = mark_tool_managed(state, "opencode", managed_keys)
    save_state(state)
    return state, token


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A `local` MCP server runs a command over stdio; `command` is the full
    # argv. ug registers the `ug mcp-proxy ...` bridge here so OpenCode
    # never speaks HTTP+bearer directly — the proxy mints fresh tokens itself.
    return {
        "type": "local",
        "command": list(argv),
        "enabled": True,
    }


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    backup_existing_file(OPENCODE_CONFIG_PATH, OPENCODE_BACKUP_PATH)
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    removed = name in mcp_servers
    mcp_servers[name] = build_mcp_server_entry(argv)
    existing["mcp"] = mcp_servers
    write_json_file(OPENCODE_CONFIG_PATH, existing)
    return removed


def remove_mcp_server_config(name: str) -> bool:
    existing = read_json_safe(OPENCODE_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    mcp_servers.pop(name)
    existing["mcp"] = mcp_servers
    write_json_file(OPENCODE_CONFIG_PATH, existing)
    return True


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to OpenCode's `mcp` table in a single read-modify-write. Returns the names actually removed."""
    return apply_json_mcp_diff(
        OPENCODE_CONFIG_PATH, "mcp", add, remove, backup_path=OPENCODE_BACKUP_PATH
    )


def default_model(state: dict) -> str | None:
    return opencode_family.default_model(_SETTINGS, state)


def _configure_launch(state: dict, model: str | None = None) -> str:
    model = model or default_model(state)
    if not model:
        raise RuntimeError("No OpenCode model is configured.")
    _, token = write_tool_config(state, model)
    return token


def build_runtime_env(token: str, state: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    env["XDG_CONFIG_HOME"] = str(OPENCODE_XDG_CONFIG_HOME)
    return env


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch OpenCode with the selected model and on-demand token refresh."""
    model = explicit_model_arg_value(tool_args) or options.user_pinned_model
    if model is not None:
        model = resolve_explicit_model(model, state)
    token = _configure_launch(state, model)
    env = build_runtime_env(token, state)

    if model is not None and not has_explicit_model_arg(tool_args):
        try:
            separator = tool_args.index("--")
        except ValueError:
            separator = len(tool_args)
        tool_args = [
            *tool_args[:separator],
            "--model",
            model,
            *tool_args[separator:],
        ]

    metrics.agent_handoff()
    proc = subprocess_cross_os.popen([SPEC["binary"], *tool_args], env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "run", "say hi in 5 words or less"]
