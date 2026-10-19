"""GitHub Copilot CLI agent: writes ~/.copilot/ucode.env and injects BYOK env vars at launch.

Copilot CLI's BYOK config (COPILOT_PROVIDER_*) is documented as env-var only —
the CLI does not auto-load ~/.copilot/ucode.env. We still write the file so users can
inspect what's configured and to give `revert` something to clean up; it never holds
a credential.

Copilot holds its provider credential for the whole session, so `ug copilot` runs it
against a per-session loopback proxy (`gateway_proxy.start_gateway_auth_proxy`):
Copilot authenticates to the proxy with a random session secret, and the proxy
forwards each request with a fresh Databricks token.

System Claude models (`system.ai.claude-*`) use Copilot CLI's `anthropic` provider against the gateway's
Anthropic endpoint, with `COPILOT_PROVIDER_MODEL_ID` set to Copilot's catalog id
for the model so Copilot sends Claude request settings. Other models use the
`openai` provider against the Databricks MLflow gateway: GPT models with major
version 5 or newer use Responses, because Copilot sends `temperature: 0` on Chat
Completions and those models accept only the default; the rest use Chat Completions. Copilot fixes its wire API and model when it builds the native
client, so changing models in the picker cannot change either mid-session.
Relaunch Copilot after changing model families. Gemini is intentionally excluded
— Databricks' Gemini translation layer rejects the `stream_options` field that
Copilot CLI sends, so Gemini models 400 on every request.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    apply_json_mcp_diff,
    backup_existing_file,
    parse_dotenv,
    read_json_safe,
    write_dotenv,
    write_json_file,
)
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER, LOOPBACK_HOST
from ucode.custom_oauth import get_custom_client_token
from ucode.databricks import (
    build_copilot_base_url,
    build_tool_base_url,
    get_databricks_token,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version

from .args import LaunchOptions, explicit_model_arg_value
from .request_tags import request_tags_header_value

COPILOT_CONFIG_DIR = Path.home() / ".copilot"
COPILOT_ENV_PATH = COPILOT_CONFIG_DIR / "ucode.env"
COPILOT_MCP_CONFIG_PATH = COPILOT_CONFIG_DIR / "ucode-mcp-config.json"
COPILOT_BACKUP_PATH = APP_DIR / "copilot-ucode-env.backup"
COPILOT_MCP_BACKUP_PATH = APP_DIR / "copilot-ucode-mcp-config.backup.json"
COPILOT_PROXY_LOG_PATH = APP_DIR / "logs" / "copilot-proxy.log"

SPEC: ToolSpec = {
    "binary": "copilot",
    "package": "@github/copilot",
    "display": "GitHub Copilot CLI",
    "config_path": COPILOT_ENV_PATH,
    "backup_path": COPILOT_BACKUP_PATH,
}

MANAGED_KEYS: list[str] = [
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_MODEL",
    "COPILOT_OFFLINE",
]
LEGACY_ENV_KEYS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "COPILOT_PROVIDER_API_KEY",
]
# Earlier versions wrote the Databricks token (or PAT) under these keys.
CREDENTIAL_ENV_KEYS = ("COPILOT_PROVIDER_BEARER_TOKEN", "OAUTH_TOKEN")
# Inherited settings that would bypass the proxy's credential or its HTTP transport.
_INHERITED_PROVIDER_OVERRIDES = (
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_API_KEY_COMMAND",
    "COPILOT_PROVIDER_TRANSPORT",
)
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^(?:system\.ai\.)?(?:databricks-)?gpt-(\d+)(?=$|[.-])")


_SYSTEM_CLAUDE_MODEL_PATTERN = re.compile(r"^system\.ai\.(claude-[^\[]+)(?:\[[^\]]*\])?$")
_CLAUDE_DASHED_VERSION_PATTERN = re.compile(r"-(\d+)-(\d+)$")
_CLAUDE_HAIKU_MAJOR_PATTERN = re.compile(r"^claude-haiku-(\d+)")


def copilot_catalog_model_id(model: str) -> str | None:
    """Copilot's catalog id for a system Claude model, e.g. ``system.ai.claude-sonnet-5-5`` -> ``claude-sonnet-5.5``.

    Copilot applies Claude request settings to any ``claude-*`` id, so ids missing from its catalog still work.
    Returns None for anything other than ``system.ai.claude-*``.
    """
    match = _SYSTEM_CLAUDE_MODEL_PATTERN.match(model)
    if match is None:
        return None
    catalog_id = _CLAUDE_DASHED_VERSION_PATTERN.sub(r"-\1.\2", match.group(1))
    # Copilot sends budget thinking for any Haiku it doesn't know, which Haiku 5+ rejects; Sonnet
    # ids get the adaptive thinking those models require.
    haiku = _CLAUDE_HAIKU_MAJOR_PATTERN.match(catalog_id)
    if haiku and int(haiku.group(1)) >= 5:
        catalog_id = catalog_id.replace("claude-haiku-", "claude-sonnet-", 1)
    return catalog_id


def model_uses_responses_api(model: str) -> bool:
    """Whether a supported GPT model id uses the Responses API."""
    match = _GPT_MODEL_MAJOR_PATTERN.match(model)
    return match is not None and int(match.group(1)) >= 5


def default_model(state: dict) -> str | None:
    """Prefer Claude sonnet, then opus/haiku, then codex.

    A managed config's ``copilot_default_model`` and ``copilot_models`` both win outright: the former is
    the admin's chosen session start, the latter their allowlist. Workspace-wide discovery falls back.
    """
    if isinstance(state.get("copilot_default_model"), str):
        return state.get("copilot_default_model")
    copilot_models = state.get("copilot_models") or []
    if isinstance(copilot_models, list) and copilot_models:
        return copilot_models[0]
    claude_models = state.get("claude_models") or {}
    for family in ("sonnet", "opus", "haiku"):
        if claude_models.get(family):
            return claude_models[family]
    codex_models = state.get("codex_models") or []
    if codex_models:
        return codex_models[0]
    return next(iter(claude_models.values()), None)


def render_env_overlay(
    base_url: str,
    selected_model: str,
    *,
    override_model: str | None = None,
) -> dict[str, str]:
    """Copilot's provider settings for ``selected_model``, rooted at ``base_url``.

    ``base_url`` is the workspace for the inspectable env file and the loopback proxy
    at launch. Credentials are added only to the launch environment."""
    request_model = override_model or selected_model
    catalog_model_id = copilot_catalog_model_id(request_model)
    if catalog_model_id is not None:
        return {
            "COPILOT_PROVIDER_TYPE": "anthropic",
            "COPILOT_PROVIDER_BASE_URL": build_tool_base_url("claude", base_url),
            "COPILOT_PROVIDER_MODEL_ID": catalog_model_id,
            "COPILOT_MODEL": selected_model,
            "COPILOT_OFFLINE": "true",
        }
    wire_api = "responses" if model_uses_responses_api(request_model) else "completions"
    return {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": build_copilot_base_url(base_url),
        "COPILOT_PROVIDER_WIRE_API": wire_api,
        "COPILOT_MODEL": selected_model,
        "COPILOT_OFFLINE": "true",
    }


def build_runtime_env(proxy_base_url: str, model: str, session_secret: str) -> dict[str, str]:
    """Copilot's launch environment: provider settings pointed at the session proxy."""
    env = os.environ.copy()
    for key in _INHERITED_PROVIDER_OVERRIDES:
        env.pop(key, None)
    override_model = env.get("COPILOT_PROVIDER_WIRE_MODEL")
    env.update(render_env_overlay(proxy_base_url, model, override_model=override_model))
    env["COPILOT_PROVIDER_BEARER_TOKEN"] = session_secret
    return env


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A `local` MCP server runs a stdio command; `command`/`args` split the
    # argv. ug registers the `ug mcp-proxy ...` bridge here so Copilot
    # never speaks HTTP+bearer directly — the proxy handles token refresh.
    return {
        "type": "local",
        "command": argv[0],
        "args": list(argv[1:]),
        "tools": ["*"],
    }


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    backup_existing_file(COPILOT_MCP_CONFIG_PATH, COPILOT_MCP_BACKUP_PATH)
    existing = read_json_safe(COPILOT_MCP_CONFIG_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    removed = name in mcp_servers
    mcp_servers[name] = build_mcp_server_entry(argv)
    existing["mcpServers"] = mcp_servers
    write_json_file(COPILOT_MCP_CONFIG_PATH, existing)
    return removed


def remove_mcp_server_config(name: str) -> bool:
    existing = read_json_safe(COPILOT_MCP_CONFIG_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    mcp_servers.pop(name)
    existing["mcpServers"] = mcp_servers
    write_json_file(COPILOT_MCP_CONFIG_PATH, existing)
    return True


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Copilot's `mcpServers` in a single read-modify-write. Returns the names actually removed."""
    return apply_json_mcp_diff(
        COPILOT_MCP_CONFIG_PATH, "mcpServers", add, remove, backup_path=COPILOT_MCP_BACKUP_PATH
    )


def scrub_stored_credentials() -> None:
    """Remove tokens earlier versions persisted, so neither the env file nor `ug revert` keeps one.

    A backup holding a credential key was produced by ug itself (users don't own
    ucode.env), so it is dropped rather than restored."""
    if COPILOT_BACKUP_PATH.exists() and any(
        key in parse_dotenv(COPILOT_BACKUP_PATH) for key in CREDENTIAL_ENV_KEYS
    ):
        COPILOT_BACKUP_PATH.unlink()
    existing = parse_dotenv(COPILOT_ENV_PATH)
    if any(key in existing for key in CREDENTIAL_ENV_KEYS):
        for key in CREDENTIAL_ENV_KEYS:
            existing.pop(key, None)
        write_dotenv(COPILOT_ENV_PATH, existing)


def write_tool_config(state: dict, model: str) -> dict:
    scrub_stored_credentials()
    if not is_tool_managed(state, "copilot"):
        backup_existing_file(COPILOT_ENV_PATH, COPILOT_BACKUP_PATH)
    existing = parse_dotenv(COPILOT_ENV_PATH)
    # Keep the inspectable file self-consistent without treating it as launch input.
    override_model = existing.get("COPILOT_PROVIDER_WIRE_MODEL")
    overlay = render_env_overlay(state["workspace"], model, override_model=override_model)
    for key in LEGACY_ENV_KEYS:
        existing.pop(key, None)
    # Wire API is openai-only and the catalog id is anthropic-only; drop whichever this model doesn't use.
    for key in ("COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_MODEL_ID"):
        if key not in overlay:
            existing.pop(key, None)
    existing.update(overlay)
    write_dotenv(COPILOT_ENV_PATH, existing)
    state = mark_tool_managed(state, "copilot", MANAGED_KEYS)
    save_state(state)
    return state


def _token_provider(state: dict) -> Callable[[bool], str]:
    """Mint from the source the rest of ug uses for this workspace: custom OAuth when configured,
    else the CLI profile."""
    workspace = state["workspace"]
    custom_oauth = state.get("custom_oauth")

    def provider(force_refresh: bool) -> str:
        if isinstance(custom_oauth, dict):
            return get_custom_client_token(
                workspace,
                custom_oauth["client_id"],
                custom_oauth["redirect_url"],
                scopes=custom_oauth["scopes"],
                profile=custom_oauth.get("profile"),
                force_refresh=force_refresh,
            )
        return get_databricks_token(workspace, state.get("profile"), force_refresh=force_refresh)

    return provider


def proxy_headers(state: dict) -> dict[str, str]:
    """Headers the proxy adds to every gateway request; admin-managed headers win by name."""
    headers = {"User-Agent": f"ucode/{ug_version()} copilot/{agent_version(SPEC['binary'])}"}
    request_tags = request_tags_header_value()
    if request_tags:
        headers[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    managed = state.get("copilot_http_headers")
    for name, value in (managed if isinstance(managed, dict) else {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.strip().casefold() == "authorization":
            continue
        for existing in [key for key in headers if key.casefold() == name.strip().casefold()]:
            headers.pop(existing)
        headers[name.strip()] = value
    return headers


def _log_proxy_failure(exc: BaseException) -> None:
    # Copilot owns the terminal, so failures go to a file instead of stderr.
    try:
        COPILOT_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with COPILOT_PROXY_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} token refresh failed: {exc}\n")
    except OSError:
        pass


class _ChildProcess(Protocol):
    def wait(self) -> int: ...


def _wait_for_exit(proc: _ChildProcess) -> int:
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # The terminal delivers Ctrl-C to Copilot too; keep the proxy up until it exits.
            continue


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    model = explicit_model_arg_value(tool_args) or options.user_pinned_model or default_model(state)
    if not model:
        raise RuntimeError("No Copilot model is available on this workspace.")
    scrub_stored_credentials()

    # Only Copilot launches need the proxy (and its `httpx` import).
    from ucode import gateway_proxy

    workspace = state["workspace"]
    session_secret = secrets.token_urlsafe(32)
    server, cache, client = gateway_proxy.start_gateway_auth_proxy(
        workspace,
        _token_provider(state),
        session_secret=session_secret,
        added_headers=proxy_headers(state),
        sign_in_hint=f"Run `databricks auth login --host {workspace}`, then restart `ug copilot`.",
        on_refresh_failure=_log_proxy_failure,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        proxy_base_url = f"http://{LOOPBACK_HOST}:{server.server_address[1]}"
        env = build_runtime_env(proxy_base_url, model, session_secret)
        proc = subprocess_cross_os.popen([SPEC["binary"], *mcp_config_args(), *tool_args], env=env)
        returncode = _wait_for_exit(proc)
    finally:
        cache.stop()
        server.shutdown()
        server.server_close()
        client.close()
    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [
        binary,
        *mcp_config_args(),
        "--prompt",
        "say hi in 5 words or less",
        "--allow-all-tools",
    ]


def mcp_config_args() -> list[str]:
    if not COPILOT_MCP_CONFIG_PATH.exists():
        return []
    return ["--additional-mcp-config", f"@{COPILOT_MCP_CONFIG_PATH}"]
