"""Kilo agent: writes kilo.json with Databricks-backed providers.

Kilo CLI (@kilocode/cli) is a fork of OpenCode; it shares OpenCode's config
schema, provider model, and plugin system. This module mirrors opencode.py,
reading the same discovered `opencode_models` view from state, and isolating
Kilo via XDG_CONFIG_HOME (verified on kilo 7.3.1/7.4.1: Kilo reads only
$XDG_CONFIG_HOME/kilo/ and ignores the user's real ~/.config/kilo)."""

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

KILO_XDG_CONFIG_HOME = APP_DIR / "kilo-xdg"
KILO_CONFIG_DIR = KILO_XDG_CONFIG_HOME / "kilo"
KILO_CONFIG_PATH = KILO_CONFIG_DIR / "kilo.json"
KILO_BACKUP_PATH = APP_DIR / "kilo-config.backup.json"
KILO_AUTH_PLUGIN_PATH = KILO_CONFIG_DIR / "plugin" / "ucode-auth.js"
KILO_NPM_PACKAGE = "@kilocode/cli"
# Minimum Kilo version based on tests is 7.3.1
MINIMUM_KILO_VERSION = (7, 3, 1)
MINIMUM_KILO_VERSION_TEXT = "7.3.1"

SPEC: ToolSpec = {
    "binary": "kilo",
    "package": f"{KILO_NPM_PACKAGE}@7",
    "display": "Kilo",
    "config_path": KILO_CONFIG_PATH,
    "backup_path": KILO_BACKUP_PATH,
}

PROVIDER_KEYS: list[list[str]] = opencode_family.provider_keys()


_SETTINGS = opencode_family.FamilySettings(
    display="Kilo",
    default_model_state_key=None,
    empty_models_hint=(
        "Run `ug configure --agent kilo` to discover models, then choose a "
        "discovered model id or pass a provider/model selector."
    ),
)

_parse_version = opencode_family.parse_version


def _minimum_version_message() -> str | None:
    return opencode_family.minimum_version_message(
        _SETTINGS,
        agent_version(SPEC["binary"]),
        MINIMUM_KILO_VERSION,
        MINIMUM_KILO_VERSION_TEXT,
    )


def minimum_version_error() -> str | None:
    return opencode_family.minimum_version_error(_minimum_version_message(), SPEC["package"])


def render_auth_plugin(state: dict) -> str:
    """Render the local Kilo plugin that refreshes Databricks auth on demand."""
    # Kilo stays on the V1 entrypoint.
    return opencode_family.render_auth_plugin(state, v2=False)


def write_auth_plugin(state: dict) -> None:
    """Install the auto-discovered hook in ucode's isolated Kilo config."""
    # Derive the path so tests that redirect KILO_CONFIG_PATH stay isolated.
    path = KILO_CONFIG_PATH.parent / "plugin" / KILO_AUTH_PLUGIN_PATH.name
    write_text_file(path, render_auth_plugin(state))


_resolve_model_selector = opencode_family.resolve_model_selector


def resolve_explicit_model(model: str, state: dict) -> str:
    """Resolve and validate a model explicitly selected for a Kilo launch."""
    return opencode_family.resolve_explicit_model(_SETTINGS, model, state)


def _oss_model_overlay(model: str, ua_header: dict[str, str]) -> dict:
    """Per-model overlay for an OSS model entry."""
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
    """Return (overlay, managed_key_paths) for kilo.json."""
    auth_headers = {"Authorization": f"Bearer {token}"}
    # The User-Agent must be set on each per-model `headers` entry; a
    # provider-level `headers["User-Agent"]` does not take effect.
    ua_header = {
        "User-Agent": ucode_user_agent(ug_version(), f"kilo/{agent_version('kilo')}"),
    }
    # Request tags go on the per-model headers with the User-Agent.
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
    backup_existing_file(KILO_CONFIG_PATH, KILO_BACKUP_PATH)
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
    existing = read_json_safe(KILO_CONFIG_PATH)
    write_auth_plugin(state)
    providers = existing.get("provider")
    if isinstance(providers, dict):
        for stale in (
            "databricks-anthropic",
            "databricks-google",
            "databricks-oss",
        ):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_json_file(KILO_CONFIG_PATH, merged)
    state = mark_tool_managed(state, "kilo", managed_keys)
    save_state(state)
    return state, token


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A `local` MCP server runs a command over stdio; `command` is the full
    # argv. ug registers the `ug mcp-proxy ...` bridge here so Kilo
    # never speaks HTTP+bearer directly — the proxy mints fresh tokens itself.
    return {
        "type": "local",
        "command": list(argv),
        "enabled": True,
    }


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    backup_existing_file(KILO_CONFIG_PATH, KILO_BACKUP_PATH)
    existing = read_json_safe(KILO_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    removed = name in mcp_servers
    mcp_servers[name] = build_mcp_server_entry(argv)
    existing["mcp"] = mcp_servers
    write_json_file(KILO_CONFIG_PATH, existing)
    return removed


def remove_mcp_server_config(name: str) -> bool:
    existing = read_json_safe(KILO_CONFIG_PATH)
    mcp_servers = existing.get("mcp")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    mcp_servers.pop(name)
    existing["mcp"] = mcp_servers
    write_json_file(KILO_CONFIG_PATH, existing)
    return True


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Kilo's `mcp` table in a single read-modify-write. Returns the names actually removed."""
    return apply_json_mcp_diff(KILO_CONFIG_PATH, "mcp", add, remove, backup_path=KILO_BACKUP_PATH)


def default_model(state: dict) -> str | None:
    return opencode_family.default_model(_SETTINGS, state)


def _configure_launch(state: dict, model: str | None = None) -> str:
    model = model or default_model(state)
    if not model:
        raise RuntimeError("No Kilo model is configured.")
    _, token = write_tool_config(state, model)
    return token


def build_runtime_env(token: str, state: dict | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    # Isolation: like OpenCode, Kilo honors XDG_CONFIG_HOME. Pointing it at our
    # private dir makes Kilo read ONLY $XDG_CONFIG_HOME/kilo/ and ignore the user's
    # real ~/.config/kilo. Verified on kilo 7.3.1/7.4.1: config() plugin hook fires
    # (token refresh works) and `kilo mcp list` sees only our managed servers.
    # Note: KILO_CONFIG merely MERGES on top of the real config (not exclusive), so
    # it is not used for isolation.
    env["XDG_CONFIG_HOME"] = str(KILO_XDG_CONFIG_HOME)
    return env


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch Kilo with the selected model and on-demand token refresh."""
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
