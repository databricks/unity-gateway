"""GitHub Copilot CLI agent: writes ~/.copilot/.env and injects BYOK env vars at launch.

Copilot CLI's BYOK config (COPILOT_PROVIDER_*) is documented as env-var only —
the CLI does not auto-load ~/.copilot/.env. We still write the file so users can
inspect what's configured (`cat ~/.copilot/.env`) and to give `revert` something
to clean up; the values are also injected directly into the child process's
environment at launch.

Claude models use Copilot CLI's `anthropic` provider against the gateway's
Anthropic endpoint, with `COPILOT_PROVIDER_MODEL_ID` set to Copilot's catalog id
for the model so Copilot sends Claude request settings. Other models use the
`openai` provider against the Databricks MLflow gateway: GPT models with major
version 6 or newer use Responses; the rest use Chat Completions. Copilot fixes its wire API and model when it builds the native
client, so changing models in the picker cannot change either mid-session.
Relaunch Copilot after changing model families. Gemini is intentionally excluded
— Databricks' Gemini translation layer rejects the `stream_options` field that
Copilot CLI sends, so Gemini models 400 on every request.
"""

from __future__ import annotations

import os
import re
import signal
import threading
from pathlib import Path

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
from ucode.databricks import (
    TOKEN_REFRESH_INTERVAL_SECONDS,
    build_copilot_base_url,
    build_tool_base_url,
    get_databricks_token,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state

from .args import LaunchOptions, explicit_model_arg_value

COPILOT_CONFIG_DIR = Path.home() / ".copilot"
COPILOT_ENV_PATH = COPILOT_CONFIG_DIR / "ucode.env"
COPILOT_MCP_CONFIG_PATH = COPILOT_CONFIG_DIR / "ucode-mcp-config.json"
COPILOT_BACKUP_PATH = APP_DIR / "copilot-ucode-env.backup"
COPILOT_MCP_BACKUP_PATH = APP_DIR / "copilot-ucode-mcp-config.backup.json"

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
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_OFFLINE",
    "OAUTH_TOKEN",
]
LEGACY_ENV_KEYS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "COPILOT_PROVIDER_API_KEY",
]
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^(?:system\.ai\.)?(?:databricks-)?gpt-(\d+)(?=$|[.-])")


_CLAUDE_MODEL_PATTERN = re.compile(r"(?:^|[./-])(claude-.+)$")
_CLAUDE_VERSION_SUFFIX_PATTERN = re.compile(r"(?:\[[^\]]*\]|-v\d+(?::\d+)?|-\d{8})+$")
_CLAUDE_DASHED_VERSION_PATTERN = re.compile(r"-(\d+)-(\d+)$")


def copilot_catalog_model_id(model: str) -> str | None:
    """Copilot's catalog id for a Claude model id, e.g. ``system.ai.claude-sonnet-5-5`` -> ``claude-sonnet-5.5``.

    Copilot applies Claude request settings to any ``claude-*`` id, so ids missing from its catalog still work.
    Returns None for non-Claude models.
    """
    match = _CLAUDE_MODEL_PATTERN.search(model)
    if match is None:
        return None
    catalog_id = _CLAUDE_VERSION_SUFFIX_PATTERN.sub("", match.group(1))
    return _CLAUDE_DASHED_VERSION_PATTERN.sub(r"-\1.\2", catalog_id)


def model_uses_responses_api(model: str) -> bool:
    """Whether a supported GPT model id uses the Responses API."""
    match = _GPT_MODEL_MAJOR_PATTERN.match(model)
    return match is not None and int(match.group(1)) >= 6


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
    workspace: str,
    selected_model: str,
    token: str,
    *,
    override_model: str | None = None,
) -> dict[str, str]:
    request_model = override_model or selected_model
    catalog_model_id = copilot_catalog_model_id(request_model)
    if catalog_model_id is not None:
        return {
            "COPILOT_PROVIDER_TYPE": "anthropic",
            "COPILOT_PROVIDER_BASE_URL": build_tool_base_url("claude", workspace),
            "COPILOT_PROVIDER_MODEL_ID": catalog_model_id,
            "COPILOT_MODEL": selected_model,
            "COPILOT_PROVIDER_BEARER_TOKEN": token,
            "COPILOT_OFFLINE": "true",
            "OAUTH_TOKEN": token,
        }
    wire_api = "responses" if model_uses_responses_api(request_model) else "completions"
    return {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": build_copilot_base_url(workspace),
        "COPILOT_PROVIDER_WIRE_API": wire_api,
        "COPILOT_MODEL": selected_model,
        "COPILOT_PROVIDER_BEARER_TOKEN": token,
        "COPILOT_OFFLINE": "true",
        "OAUTH_TOKEN": token,
    }


def build_runtime_env(workspace: str, model: str, token: str) -> dict[str, str]:
    env = os.environ.copy()
    override_model = env.get("COPILOT_PROVIDER_WIRE_MODEL")
    env.update(render_env_overlay(workspace, model, token, override_model=override_model))
    return env


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A `local` MCP server runs a stdio command; `command`/`args` split the
    # argv. ug registers the `ug mcp-proxy ...` bridge here so Copilot
    # never speaks HTTP+bearer directly — the proxy handles token refresh. The
    # OAUTH_TOKEN env Copilot still injects at launch is for MODEL auth, not MCP.
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


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[dict, str]:
    backup_existing_file(COPILOT_ENV_PATH, COPILOT_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(
            state["workspace"], state.get("profile"), force_refresh=force_refresh
        )
    existing = parse_dotenv(COPILOT_ENV_PATH)
    # Keep the inspectable file self-consistent without treating it as launch input.
    override_model = existing.get("COPILOT_PROVIDER_WIRE_MODEL")
    overlay = render_env_overlay(state["workspace"], model, token, override_model=override_model)
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
    return state, token


def _refresh_token_once(
    state: dict,
    model: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[str, str]:
    model = model or default_model(state)
    if not model:
        raise RuntimeError("No Copilot model is available on this workspace.")
    _, token = write_tool_config(state, model, force_refresh=force_refresh)
    return model, token


def _refresh_forever(state: dict, model: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_token_once(state, model, force_refresh=True)
        except RuntimeError:
            continue


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    model = explicit_model_arg_value(tool_args) or options.user_pinned_model or default_model(state)
    model, token = _refresh_token_once(state, model)
    env = build_runtime_env(state["workspace"], model, token)

    stop_event = threading.Event()
    refresher = threading.Thread(
        target=_refresh_forever,
        args=(state, model, stop_event),
        daemon=True,
    )
    refresher.start()

    proc = subprocess_cross_os.popen([SPEC["binary"], *mcp_config_args(), *tool_args], env=env)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    finally:
        stop_event.set()
        refresher.join(timeout=1)

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
