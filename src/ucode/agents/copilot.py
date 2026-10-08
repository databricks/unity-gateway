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
import shutil
import signal
import subprocess
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
_GITHUB_COPILOT_BRAND = re.compile(r"\bGitHub Copilot CLI\b", re.IGNORECASE)
_COPILOT_VERSION_PROBE_TIMEOUT_SECONDS = 2
_WINDOWS_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")


class CopilotBinaryConflictError(RuntimeError):
    """Raised when ``copilot`` resolves only to a different product's CLI."""


def _windows_copilot_path_candidate(path_entry: str) -> str | None:
    """Resolve only inside one PATH entry, without Windows prepending the current directory."""
    raw_extensions = os.environ.get("PATHEXT")
    extensions = (
        [extension for extension in raw_extensions.split(os.pathsep) if extension]
        if raw_extensions
        else _WINDOWS_DEFAULT_PATHEXT
    )
    for extension in extensions:
        candidate = os.path.join(path_entry, f"{SPEC['binary']}{extension}")
        if os.path.isfile(candidate):
            return candidate
    return None


def _copilot_path_candidate(path_entry: str) -> str | None:
    if os.name == "nt":
        return _windows_copilot_path_candidate(path_entry)
    return shutil.which(SPEC["binary"], path=path_entry)


def _copilot_path_candidates() -> list[str]:
    """Return each distinct ``copilot`` executable found on PATH, in PATH order."""
    path = os.environ.get("PATH", os.defpath)
    candidates: list[str] = []
    seen: set[str] = set()
    for path_entry in path.split(os.pathsep):
        if not path_entry:
            continue
        candidate = _copilot_path_candidate(path_entry)
        if not candidate:
            continue
        absolute = os.path.abspath(candidate)
        key = os.path.normcase(absolute)
        if key not in seen:
            seen.add(key)
            candidates.append(absolute)
    return candidates


def _is_github_copilot_cli(binary: str) -> bool:
    try:
        result = subprocess_cross_os.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_COPILOT_VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    return _GITHUB_COPILOT_BRAND.search(output) is not None


def resolve_binary() -> str | None:
    """Find and verify the GitHub Copilot CLI, rather than another ``copilot`` command."""
    candidates = _copilot_path_candidates()
    for candidate in candidates:
        if _is_github_copilot_cli(candidate):
            return candidate
    if candidates:
        locations = ", ".join(candidates)
        raise CopilotBinaryConflictError(
            "Found `copilot` on PATH, but none reported the GitHub Copilot CLI "
            f"({locations}). AWS Copilot uses the same command name; remove or reorder the "
            "conflicting executable, then install GitHub Copilot CLI with `npm install -g "
            "@github/copilot`."
        )
    return None


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
    binary = resolve_binary()
    if binary is None:
        raise RuntimeError(
            "GitHub Copilot CLI is not installed (`copilot` was not found on PATH). "
            "Install it with `npm install -g @github/copilot` and retry."
        )
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

    proc = subprocess_cross_os.popen([binary, *mcp_config_args(), *tool_args], env=env)
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
