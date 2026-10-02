"""GitHub Copilot CLI agent: writes ~/.copilot/.env and injects BYOK env vars at launch.

Copilot CLI's BYOK config (COPILOT_PROVIDER_*) is documented as env-var only —
the CLI does not auto-load ~/.copilot/.env. We still write the file so users can
inspect what's configured (`cat ~/.copilot/.env`) and to give `revert` something
to clean up; the values are also injected directly into the child process's
environment at launch.

We point Copilot CLI's `openai` provider at the Databricks MLflow gateway. GPT
models with major version 6 or newer use Responses; other models use Chat
Completions. Copilot fixes its wire API and model when it builds the native
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
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER
from ucode.databricks import (
    TOKEN_REFRESH_INTERVAL_SECONDS,
    build_copilot_base_url,
    build_tool_base_url,
    get_databricks_token,
    resolve_provider_launch_model,
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
    "COPILOT_MODEL",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_HEADERS",
    "COPILOT_OFFLINE",
    "OAUTH_TOKEN",
]
LEGACY_ENV_KEYS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "COPILOT_PROVIDER_API_KEY",
]
_CLAUDE_MODEL_PATTERN = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2})(?!\d))?")
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^(?:system\.ai\.)?(?:databricks-)?gpt-(\d+)(?=$|[.-])")


def model_uses_responses_api(model: str) -> bool:
    """Whether a supported GPT model id uses the Responses API."""
    match = _GPT_MODEL_MAJOR_PATTERN.match(model)
    return match is not None and int(match.group(1)) >= 6


def canonical_claude_model_id(model: str) -> str | None:
    """Copilot's well-known id (``claude-sonnet-4.6``) for a Claude model id, if it names one.

    Accepts canonical and Bedrock-style ids (``us.anthropic.claude-sonnet-4-6``). Copilot uses the
    canonical form for token counting and limits, which it can't derive from a provider slug.
    """
    match = _CLAUDE_MODEL_PATTERN.search(model)
    if match is None:
        return None
    family, major, minor = match.groups()
    return f"claude-{family}-{major}.{minor}" if minor else f"claude-{family}-{major}"


def resolve_provider_model(requested: str | None, provider_models: dict[str, str]) -> str | None:
    """Pick the provider target a Copilot launch starts on.

    Like Claude's resolution, but a requested canonical id (e.g. a managed ``default_model`` of
    ``claude-sonnet-4-6``) is matched to the service's own slug for that model when it has one.
    """
    if requested and requested not in provider_models:
        wanted = canonical_claude_model_id(requested)
        for target in provider_models.values():
            if wanted and canonical_claude_model_id(target) == wanted:
                return target
    return resolve_provider_launch_model(requested, provider_models)


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
    provider: str | None = None,
) -> dict[str, str]:
    if provider:
        # A Model Provider Service (e.g. Bedrock-backed) is only reachable through the gateway's
        # Anthropic endpoint; the OpenAI-style MLflow route rejects it.
        overlay = {
            "COPILOT_PROVIDER_TYPE": "anthropic",
            "COPILOT_PROVIDER_BASE_URL": build_tool_base_url("claude", workspace),
            "COPILOT_PROVIDER_HEADERS": f"{MODEL_PROVIDER_SERVICE_HEADER}: {provider}",
            "COPILOT_MODEL": selected_model,
            "COPILOT_PROVIDER_BEARER_TOKEN": token,
            "COPILOT_OFFLINE": "true",
            "OAUTH_TOKEN": token,
        }
        model_id = canonical_claude_model_id(selected_model)
        if model_id:
            overlay["COPILOT_PROVIDER_MODEL_ID"] = model_id
        return overlay
    request_model = override_model or selected_model
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


def build_runtime_env(
    workspace: str, model: str, token: str, *, provider: str | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    override_model = env.get("COPILOT_PROVIDER_WIRE_MODEL")
    env.update(
        render_env_overlay(
            workspace, model, token, override_model=override_model, provider=provider
        )
    )
    if provider:
        env.pop("COPILOT_PROVIDER_WIRE_API", None)
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
    provider: str | None = None,
) -> tuple[dict, str]:
    backup_existing_file(COPILOT_ENV_PATH, COPILOT_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(
            state["workspace"], state.get("profile"), force_refresh=force_refresh
        )
    existing = parse_dotenv(COPILOT_ENV_PATH)
    # Keep the inspectable file self-consistent without treating it as launch input.
    override_model = existing.get("COPILOT_PROVIDER_WIRE_MODEL")
    overlay = render_env_overlay(
        state["workspace"], model, token, override_model=override_model, provider=provider
    )
    for key in LEGACY_ENV_KEYS:
        existing.pop(key, None)
    if provider:
        existing.pop("COPILOT_PROVIDER_WIRE_API", None)
    else:
        existing.pop("COPILOT_PROVIDER_HEADERS", None)
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
    provider: str | None = None,
) -> tuple[str, str]:
    model = model or default_model(state)
    if not model:
        raise RuntimeError("No Copilot model is available on this workspace.")
    _, token = write_tool_config(state, model, force_refresh=force_refresh, provider=provider)
    return model, token


def _refresh_forever(
    state: dict, model: str, stop_event: threading.Event, provider: str | None = None
) -> None:
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL_SECONDS):
        try:
            _refresh_token_once(state, model, force_refresh=True, provider=provider)
        except RuntimeError:
            continue


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    model = explicit_model_arg_value(tool_args) or options.user_pinned_model or default_model(state)
    provider = state.get("_copilot_launch_provider")
    model, token = _refresh_token_once(state, model, provider=provider)
    env = build_runtime_env(state["workspace"], model, token, provider=provider)

    stop_event = threading.Event()
    refresher = threading.Thread(
        target=_refresh_forever,
        args=(state, model, stop_event, provider),
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
