"""GitHub Copilot CLI agent: writes ~/.copilot/.env and injects BYOK env vars at launch.

Copilot CLI's BYOK config (COPILOT_PROVIDER_*) is documented as env-var only —
the CLI does not auto-load ~/.copilot/.env. We still write the file so users can
inspect what's configured (`cat ~/.copilot/.env`) and to give `revert` something
to clean up; the values are also injected directly into the child process's
environment at launch.

Claude uses Copilot's native `anthropic` provider so its cache_control markers
reach the Databricks Messages gateway. Copilot 1.0.81-6 or newer is required;
older versions retain the OpenAI-compatible route. GPT uses the MLflow gateway:
GPT-6+ uses Responses and older GPT uses Chat Completions. Switching model
families requires relaunching Copilot. Gemini remains unsupported because its
gateway translation rejects Copilot's stream_options.
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
    build_copilot_base_urls,
    get_databricks_token,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version

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
    "COPILOT_OFFLINE",
    "OAUTH_TOKEN",
]
LEGACY_ENV_KEYS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "COPILOT_PROVIDER_API_KEY",
]
# COPILOT_MODEL (openai) vs COPILOT_PROVIDER_MODEL_ID+COPILOT_PROVIDER_WIRE_MODEL
# (anthropic) are mutually exclusive — cleared before every write so switching
# families doesn't leave the other set stale in ~/.copilot/ucode.env.
_MODEL_SELECTION_KEYS = (
    "COPILOT_MODEL",
    "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL",
)

_CANONICAL_CLAUDE_MODEL_ID_RE = re.compile(r"claude-[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
# Copilot's model catalog does not use Bedrock's `-v1:0` suffix.
_BEDROCK_VERSION_SUFFIX_RE = re.compile(r"-v\d+(:\d+)?$")

# (major, minor, patch, prerelease) — see the module docstring. A version with
# no prerelease suffix (a final release) is a 4th component of _UNRELEASED so
# it always sorts after every prerelease of the same (major, minor, patch).
MINIMUM_COPILOT_ANTHROPIC_VERSION = (1, 0, 81, 6)
_UNRELEASED = 999_999
_COPILOT_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-(\d+))?")
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^(?:system\.ai\.)?(?:databricks-)?gpt-(\d+)(?=$|[.-])")


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


def _is_claude_model(model: str) -> bool:
    # Every Claude family/model id ucode discovers or pins contains "claude"
    # (canonical Anthropic names like "claude-sonnet-5", or Bedrock-style
    # slugs like "us.anthropic.claude-opus-4-8") — same substring check
    # `databricks.py` already uses elsewhere to special-case the family.
    return "claude" in model.lower()


def _canonical_claude_model_id(model: str) -> str:
    # e.g. "system.ai.claude-sonnet-5" -> "claude-sonnet-5" — the well-known
    # name Copilot needs to recognize the model (see render_env_overlay).
    # Lowercased and stripped of any Bedrock version suffix so it matches
    # Copilot's catalog regardless of the input's casing or source.
    match = _CANONICAL_CLAUDE_MODEL_ID_RE.search(model)
    canonical = match.group(0).lower() if match else model.lower()
    return _BEDROCK_VERSION_SUFFIX_RE.sub("", canonical)


def _parse_copilot_version(value: str) -> tuple[int, int, int, int] | None:
    match = _COPILOT_VERSION_RE.search(value)
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), int(pre) if pre is not None else _UNRELEASED


def _supports_anthropic_provider() -> bool:
    version = _parse_copilot_version(agent_version(SPEC["binary"]))
    return version is not None and version >= MINIMUM_COPILOT_ANTHROPIC_VERSION


def render_env_overlay(
    workspace: str,
    model: str,
    token: str,
    *,
    override_model: str | None = None,
) -> dict[str, str]:
    base_urls = build_copilot_base_urls(workspace)
    if _is_claude_model(model) and _supports_anthropic_provider():
        return {
            "COPILOT_PROVIDER_TYPE": "anthropic",
            "COPILOT_PROVIDER_BASE_URL": base_urls["anthropic"],
            # Keep Copilot's canonical model metadata separate from the gateway wire id.
            "COPILOT_PROVIDER_MODEL_ID": _canonical_claude_model_id(model),
            "COPILOT_PROVIDER_WIRE_MODEL": model,
            "COPILOT_PROVIDER_BEARER_TOKEN": token,  # not API_KEY — see module docstring
            "COPILOT_OFFLINE": "true",
            "OAUTH_TOKEN": token,
        }
    return {
        "COPILOT_PROVIDER_TYPE": "openai",
        "COPILOT_PROVIDER_BASE_URL": base_urls["openai"],
        "COPILOT_PROVIDER_WIRE_API": (
            "responses" if model_uses_responses_api(override_model or model) else "completions"
        ),
        "COPILOT_MODEL": model,
        "COPILOT_PROVIDER_BEARER_TOKEN": token,
        "COPILOT_OFFLINE": "true",
        "OAUTH_TOKEN": token,
    }


def build_runtime_env(workspace: str, model: str, token: str) -> dict[str, str]:
    env = os.environ.copy()
    override_model = env.get("COPILOT_PROVIDER_WIRE_MODEL")
    overlay = render_env_overlay(workspace, model, token, override_model=override_model)
    for key in LEGACY_ENV_KEYS:
        env.pop(key, None)
    if overlay["COPILOT_PROVIDER_TYPE"] == "anthropic":
        for key in (
            *_MODEL_SELECTION_KEYS,
            "COPILOT_PROVIDER_MODEL_LIMITS_ID",
            "COPILOT_PROVIDER_WIRE_API",
        ):
            env.pop(key, None)
    env.update(overlay)
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
    switching_from_anthropic = existing.get("COPILOT_PROVIDER_TYPE") == "anthropic"
    for key in _MODEL_SELECTION_KEYS:
        if (
            key == "COPILOT_MODEL"
            or overlay["COPILOT_PROVIDER_TYPE"] == "anthropic"
            or switching_from_anthropic
        ):
            existing.pop(key, None)
    if overlay["COPILOT_PROVIDER_TYPE"] == "anthropic":
        existing.pop("COPILOT_PROVIDER_MODEL_LIMITS_ID", None)
        existing.pop("COPILOT_PROVIDER_WIRE_API", None)
    existing.update(overlay)
    write_dotenv(COPILOT_ENV_PATH, existing)
    managed_keys = (
        MANAGED_KEYS + ["COPILOT_PROVIDER_MODEL_ID", "COPILOT_PROVIDER_WIRE_MODEL"]
        if overlay["COPILOT_PROVIDER_TYPE"] == "anthropic"
        else MANAGED_KEYS
    )
    state = mark_tool_managed(state, "copilot", managed_keys)
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
