"""Google Antigravity CLI (`agy`) agent: launches in Enterprise Gateway mode behind a local proxy.

Antigravity CLI 1.2.15+ has an Enterprise Gateway mode, configured purely by environment
variables (`AGY_LLM_GATEWAY_URL`, `AGY_LLM_GATEWAY_API_KEY`, `AGY_LLM_GATEWAY_WIRE_PROTOCOL`,
`AGY_LLM_GATEWAY_MODELS`), that skips Google sign-in and sends standard Gemini API requests
(`POST <url>/v1beta/models/<model>:streamGenerateContent`, `Authorization: Bearer <key>`).

The CLI reads that key once at startup, so `ug antigravity` points it at a loopback
gateway-auth proxy instead: the agent holds a per-session secret, and the proxy forwards to the
workspace's Gemini route with a freshly minted Databricks token. The proxy also maps model names.
Antigravity only runs its agent loop (tools, thinking) for model ids it recognizes, which are
Google's canonical names (`gemini-3.8-flash`), so the CLI is offered those names and the proxy
rewrites each one back to its Databricks endpoint (`system.ai.gemini-3-8-flash`) in the URL.

Only Gemini models are offered: Antigravity sends no tools for model ids it doesn't know,
which rules out the gateway's Claude, GPT, and OSS models.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    apply_json_mcp_diff,
    read_json_safe,
    write_json_file,
    write_text_file,
)
from ucode.constants import (
    AI_GATEWAY_REQUEST_TAGS_HEADER,
    LOOPBACK_HOST,
    MODEL_PROVIDER_SERVICE_HEADER,
)
from ucode.custom_oauth import launch_token
from ucode.databricks import bare_model_name
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import get_provider_service, mark_tool_managed, save_state, set_provider_service
from ucode.telemetry import agent_version, ucode_user_agent, ug_version

from .args import LaunchOptions, explicit_model_arg_value, has_explicit_model_arg
from .request_tags import request_tags_header_value

AGY_BINARY = "agy"
AGY_INSTALL_SCRIPT_URL = "https://antigravity.google/cli/install.sh"
# Global customization root (`~/.gemini/config/`) shared by the Antigravity app, CLI, and IDE
# extensions; `agy mcp add` writes here too.
AGY_CONFIG_DIR = Path.home() / ".gemini" / "config"
AGY_MCP_CONFIG_PATH = AGY_CONFIG_DIR / "mcp_config.json"
AGY_SKILLS_CONFIG_PATH = AGY_CONFIG_DIR / "skills.json"
# skills.json is shared with the Antigravity app and IDE extensions, so ug never snapshots and
# restores it: revert removes only ug's entry. This marker records that ug created the file.
AGY_SKILLS_CREATED_MARKER = APP_DIR / "antigravity-skills.created"
AGY_MCP_BACKUP_PATH = APP_DIR / "antigravity-mcp-config.backup.json"
AGY_PROXY_LOG_PATH = APP_DIR / "logs" / "antigravity-proxy.log"
# Where ug downloads skills for every non-Claude agent (see skills_download). Antigravity
# discovers `.agents/skills` only inside a project, so the home copy is declared in skills.json.
UG_AGENTS_SKILLS_DIR = Path.home() / ".agents" / "skills"
# Gateway mode was added in 1.2.15.
MINIMUM_AGY_VERSION = (1, 2, 15)
MINIMUM_AGY_VERSION_TEXT = "1.2.15"
# State key for the provider-service target a provider launch pins (see `persisted_provider_model`).
PROVIDER_MODEL_STATE_KEY = "antigravity_provider_model"
GEMINI_GATEWAY_PATH = "/ai-gateway/gemini/"

SPEC: ToolSpec = {
    "binary": AGY_BINARY,
    "package": AGY_INSTALL_SCRIPT_URL,
    "display": "Antigravity CLI",
    "config_path": AGY_SKILLS_CONFIG_PATH,
    # Never written; `ug revert` calls `revert_skills_config` for this agent instead.
    "backup_path": AGY_SKILLS_CREATED_MARKER,
}

INSTALL_HINT = f"Install it with `curl -fsSL {AGY_INSTALL_SCRIPT_URL} | bash`"

MANAGED_KEYS: list[str] = ["entries"]

# `agy` subcommands that take no model; `--model` is only injected for an agent session.
_SUBCOMMANDS = frozenset(
    {
        "agent",
        "agents",
        "changelog",
        "help",
        "install",
        "mcp",
        "models",
        "plugin",
        "plugins",
        "update",
    }
)
# Databricks names some preview models without the `-preview` suffix Antigravity knows them by.
_PREVIEW_NAMES = {
    "gemini-3-flash": "gemini-3-flash-preview",
    "gemini-3.1-flash-lite": "gemini-3.1-flash-lite-preview",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
}
# Image-generation endpoints can't drive an agent session.
_NON_AGENT_MODEL_MARKERS = ("-image",)
_DOTTED_VERSION = re.compile(r"(?<=\d)-(?=\d)")
_MODEL_PATH = re.compile(r"^(?P<prefix>/ai-gateway/gemini/v1beta/models/)(?P<model>[^/:?]+)")


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _version() -> str:
    # The installer's default directory may not be on PATH yet.
    return agent_version(resolve_binary() or SPEC["binary"])


def minimum_version_error() -> str | None:
    installed = _version()
    parsed = _parse_version(installed)
    if parsed is None or parsed >= MINIMUM_AGY_VERSION:
        return None
    return (
        f"Antigravity CLI {installed} is too old. ug requires Antigravity CLI "
        f"{MINIMUM_AGY_VERSION_TEXT} or newer for Enterprise Gateway mode. "
        "Update it with `agy update`."
    )


def install_binary() -> None:
    """Install the Antigravity CLI with Google's installer script (it isn't on npm)."""
    if sys.platform == "win32":
        raise RuntimeError("Antigravity CLI Enterprise Gateway mode supports macOS and Linux only.")
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError(f"`bash` is required to install Antigravity CLI. {INSTALL_HINT}.")
    try:
        with urllib.request.urlopen(AGY_INSTALL_SCRIPT_URL, timeout=60) as response:
            script = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Could not download the Antigravity CLI installer: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="ug-agy-install-") as tmp:
        script_path = Path(tmp) / "install.sh"
        script_path.write_bytes(script)
        subprocess_cross_os.run([bash, str(script_path)], check=True, timeout=300)


def resolve_binary() -> str | None:
    """`agy` on PATH, else the installer's default location (not on PATH until a new shell)."""
    found = shutil.which(AGY_BINARY)
    if found:
        return found
    default = Path.home() / ".local" / "bin" / AGY_BINARY
    return str(default) if default.is_file() and os.access(default, os.X_OK) else None


def agy_model_name(model: str) -> str:
    """The name Antigravity knows a Databricks Gemini endpoint by.

    ``system.ai.gemini-3-8-flash`` -> ``gemini-3.8-flash``; versions are dotted and the
    catalog/schema and ``databricks-`` prefixes dropped. Names that already look like
    Google's (a provider service's targets) come back unchanged.
    """
    name = bare_model_name(model) or model
    name = _DOTTED_VERSION.sub(".", name)
    return _PREVIEW_NAMES.get(name, name)


def _is_agent_model(model: str) -> bool:
    return "gemini-" in model and not any(m in model for m in _NON_AGENT_MODEL_MARKERS)


def model_catalog(state: dict, provider: str | None = None) -> dict[str, str]:
    """``{antigravity_name: gateway_model}`` for this launch, in the order to offer them.

    Under a Model Provider Service the gateway model is the service's target, whose name
    Antigravity already knows (the persisted target leads). Otherwise it is each discovered
    Databricks Gemini endpoint; when two endpoints map to one name, the first (newest) wins.
    """
    if provider:
        model = persisted_provider_model(state)
        return {model: model} if model else {}
    catalog: dict[str, str] = {}
    for model in state.get("gemini_models") or []:
        if isinstance(model, str) and _is_agent_model(model):
            catalog.setdefault(agy_model_name(model), model)
    return catalog


def default_model(state: dict) -> str | None:
    """The Databricks model `ug antigravity` starts on: the newest discovered Gemini model."""
    return next(iter(model_catalog(state).values()), None)


def persisted_provider_model(state: dict) -> str | None:
    """The provider-service target the last provider configure or launch pinned, if any.

    Reusing it lets a bare ``ug antigravity`` relaunch run without re-passing ``--model``."""
    model = state.get(PROVIDER_MODEL_STATE_KEY)
    return model if isinstance(model, str) and model else None


def _resolve_launch_model(model: str, catalog: dict[str, str]) -> str:
    """Map a `--model` value (Databricks id, Antigravity name, or a variant such as
    ``gemini-3.8-flash-high``) to the name Antigravity is launched with."""
    if model in catalog:
        return model
    for agy_name, gateway_model in catalog.items():
        if model == gateway_model:
            return agy_name
    if any(model.startswith(f"{agy_name}-") for agy_name in catalog):
        return model
    available = ", ".join(catalog) or "none"
    raise RuntimeError(
        f"Antigravity model '{model}' is not configured. Available: {available}. "
        "Run `ug configure --agent antigravity` to rediscover models."
    )


def skills_config(existing: dict) -> dict:
    """``existing`` skills.json with ug's downloaded-skills directory declared once.

    Antigravity expands no ``~`` in these paths, so the entry is absolute."""
    config = dict(existing)
    entries = config.get("entries")
    entries = list(entries) if isinstance(entries, list) else []
    if not any(_is_ug_skills_entry(entry) for entry in entries):
        entries.append({"path": str(UG_AGENTS_SKILLS_DIR)})
    config["entries"] = entries
    return config


def _is_ug_skills_entry(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("path") == str(UG_AGENTS_SKILLS_DIR)


def revert_skills_config() -> bool:
    """Remove ug's entry from skills.json, keeping everything else the user has there.

    The file is deleted only when ug created it and nothing else was added since. Returns
    whether anything changed."""
    created = AGY_SKILLS_CREATED_MARKER.exists()
    AGY_SKILLS_CREATED_MARKER.unlink(missing_ok=True)
    existing = read_json_safe(AGY_SKILLS_CONFIG_PATH)
    entries = existing.get("entries")
    if not isinstance(entries, list) or not any(_is_ug_skills_entry(e) for e in entries):
        return False
    remaining = {
        **existing,
        "entries": [entry for entry in entries if not _is_ug_skills_entry(entry)],
    }
    if created and remaining == {"entries": []}:
        AGY_SKILLS_CONFIG_PATH.unlink(missing_ok=True)
    else:
        write_json_file(AGY_SKILLS_CONFIG_PATH, remaining)
    return True


def write_tool_config(
    state: dict,
    model: str | None,
    *,
    provider: str | None = None,
) -> dict:
    """Record the launch model and provider, and declare ug's skills directory to Antigravity.

    The gateway itself is configured per launch through environment variables, so no
    Antigravity settings file holds a URL or credential."""
    if not AGY_SKILLS_CONFIG_PATH.exists():
        write_text_file(AGY_SKILLS_CREATED_MARKER, "")
    write_json_file(AGY_SKILLS_CONFIG_PATH, skills_config(read_json_safe(AGY_SKILLS_CONFIG_PATH)))
    if provider:
        # Persist so a later bare `ug antigravity` keeps the routing header and target;
        # `--provider` on a launch overrides this saved choice.
        state = set_provider_service(state, "antigravity", provider)
        if model:
            state[PROVIDER_MODEL_STATE_KEY] = model
    state = mark_tool_managed(state, "antigravity", MANAGED_KEYS)
    save_state(state)
    return state


def configured_paths(state: dict) -> list[str]:
    return [str(AGY_SKILLS_CONFIG_PATH)]


def build_mcp_server_entry(argv: list[str]) -> dict:
    # The shape `agy mcp add <name> <command> <args...>` writes for a stdio server. ug registers
    # the `ug mcp-proxy ...` bridge here so the token is refreshed by the proxy.
    return {"command": argv[0], "args": list(argv[1:]), "disabled": False}


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Antigravity's `mcpServers` in a single read-modify-write.
    Returns the names actually removed."""
    return apply_json_mcp_diff(
        AGY_MCP_CONFIG_PATH, "mcpServers", add, remove, backup_path=AGY_MCP_BACKUP_PATH
    )


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    """Register one stdio MCP server; True when an entry of that name was replaced."""
    servers = read_json_safe(AGY_MCP_CONFIG_PATH).get("mcpServers")
    replaced = isinstance(servers, dict) and name in servers
    write_user_mcp_servers({name: build_mcp_server_entry(argv)}, set())
    return replaced


def remove_mcp_server_config(name: str) -> bool:
    servers = read_json_safe(AGY_MCP_CONFIG_PATH).get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    return bool(write_user_mcp_servers({}, {name}))


def rewrite_model_path(path: str, catalog: dict[str, str]) -> str:
    """Swap the Antigravity model name in a Gemini API path for its gateway model."""
    match = _MODEL_PATH.match(path)
    if match is None:
        return path
    model = catalog.get(unquote(match["model"]))
    if model is None:
        return path
    return f"{match['prefix']}{model}{path[match.end() :]}"


def rewrite_request_body(path: str, body: bytes) -> bytes:
    """Drop ``includeThoughts`` from a request whose thinking budget is zero.

    Antigravity sends ``{"includeThoughts": true, "thinkingBudget": 0}`` for models it treats
    as non-thinking, which Vertex rejects with a 400."""
    if not path.startswith(GEMINI_GATEWAY_PATH):
        return body
    try:
        payload = json.loads(body)
    except ValueError:
        return body
    config = payload.get("generationConfig") if isinstance(payload, dict) else None
    thinking = config.get("thinkingConfig") if isinstance(config, dict) else None
    if not isinstance(thinking, dict) or thinking.get("thinkingBudget") != 0:
        return body
    if "includeThoughts" not in thinking:
        return body
    thinking.pop("includeThoughts")
    return json.dumps(payload).encode()


def proxy_headers(provider: str | None) -> dict[str, str]:
    """Headers the proxy adds to every gateway request."""
    headers = {"User-Agent": ucode_user_agent(ug_version(), f"antigravity/{_version()}")}
    request_tags = request_tags_header_value()
    if request_tags:
        headers[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    if provider:
        # A Model Provider Service routes by this header; the URL still names its target model.
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    return headers


def build_runtime_env(proxy_url: str, session_secret: str, models: list[str]) -> dict[str, str]:
    env = os.environ.copy()
    # The CA/header overrides target an organization's own gateway, not ug's loopback proxy.
    for key in ("AGY_LLM_GATEWAY_CA_CERT", "AGY_LLM_GATEWAY_HEADERS"):
        env.pop(key, None)
    env["AGY_LLM_GATEWAY_URL"] = proxy_url
    env["AGY_LLM_GATEWAY_API_KEY"] = session_secret
    env["AGY_LLM_GATEWAY_WIRE_PROTOCOL"] = "genai"
    env["AGY_LLM_GATEWAY_MODELS"] = ",".join(models)
    return env


def _token_provider(state: dict) -> Callable[[bool], str]:
    workspace = state["workspace"]
    return lambda force_refresh: launch_token(state, workspace, force_refresh=force_refresh)


def _log_proxy_failure(exc: BaseException) -> None:
    # Antigravity owns the terminal, so failures go to a file instead of stderr.
    try:
        AGY_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AGY_PROXY_LOG_PATH.open("a", encoding="utf-8") as log:
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
            # The terminal delivers Ctrl-C to Antigravity too; keep the proxy up until it exits.
            continue


def _is_subcommand(tool_args: list[str]) -> bool:
    return bool(tool_args) and tool_args[0] in _SUBCOMMANDS


def _catalog_name(model: str, catalog: dict[str, str]) -> str | None:
    """The catalog name ``model`` (a name or one of its variants) belongs to."""
    if model in catalog:
        return model
    # `gemini-3.5-flash-lite-high` belongs to `gemini-3.5-flash-lite`, not `gemini-3.5-flash`.
    matches = [name for name in catalog if model.startswith(f"{name}-")]
    return max(matches, key=len, default=None)


def _selection_id(binary: str, env: dict[str, str], model: str, catalog: Iterable[str] = ()) -> str:
    """The id `agy --model` accepts for ``model``, read from `agy models` in gateway mode.

    Antigravity selects models by its own ids, which add a thinking tier to some names
    (``gemini-3.1-pro-preview`` is selected as ``gemini-3.1-pro-low-thinking``). A model
    offered in several tiers is also selectable by its plain name, as is any unknown one.
    ``catalog`` holds the session's other model names, whose own ids are never this model's."""
    try:
        result = subprocess_cross_os.run(
            [binary, "models"], env=env, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return model
    ids = [line.split("\t", 1)[0].strip() for line in (result.stdout or "").splitlines()]
    if model in ids:
        return model
    # Tiers are named after the model without its `-preview` suffix.
    base = model.removesuffix("-preview")
    # A longer name sharing the prefix (`gemini-3.5-flash-lite`) has its own ids and tiers.
    longer = [
        other_base
        for other in catalog
        if (other_base := other.removesuffix("-preview")).startswith(f"{base}-")
    ]
    tiers = [
        selection
        for selection in ids
        if selection.startswith(f"{base}-")
        and not any(selection == other or selection.startswith(f"{other}-") for other in longer)
    ]
    return tiers[0] if len(tiers) == 1 else model


def _with_model_arg(tool_args: list[str], model: str) -> list[str]:
    """``tool_args`` selecting ``model``: an existing ``--model`` is replaced, else one is added
    (flags precede any `--`)."""
    if has_explicit_model_arg(tool_args):
        return _replace_model_arg(tool_args, model)
    return ["--model", model, *tool_args]


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    provider = get_provider_service(state, "antigravity")
    catalog = model_catalog(state, provider)
    if not catalog:
        raise RuntimeError(
            "No Gemini model is available for Antigravity CLI on this workspace. "
            "Run `ug configure --agent antigravity` to rediscover models."
        )
    requested = explicit_model_arg_value(tool_args) or options.user_pinned_model
    selected = _resolve_launch_model(requested, catalog) if requested else None
    default = persisted_provider_model(state) if provider else default_model(state)
    start = _catalog_name(selected, catalog) if selected else None
    if start is None and default:
        start = _catalog_name(_resolve_launch_model(default, catalog), catalog)
    # Antigravity starts on the first listed model, so a plain launch passes no `--model`.
    models = list(dict.fromkeys([*([start] if start else []), *catalog]))
    binary = resolve_binary() or SPEC["binary"]

    # Only Antigravity launches need the proxy (and its `httpx` import).
    from ucode import gateway_proxy

    workspace = state["workspace"]
    session_secret = secrets.token_urlsafe(32)
    server, cache, client = gateway_proxy.start_gateway_auth_proxy(
        workspace,
        _token_provider(state),
        session_secret=session_secret,
        added_headers=proxy_headers(provider),
        sign_in_hint=f"Run `databricks auth login --host {workspace}`, then restart `ug antigravity`.",
        on_refresh_failure=_log_proxy_failure,
        allowed_path_prefixes=(GEMINI_GATEWAY_PATH,),
        path_rewriter=lambda path: rewrite_model_path(path, catalog),
        body_rewriter=rewrite_request_body,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        proxy_url = f"http://{LOOPBACK_HOST}:{port}{GEMINI_GATEWAY_PATH.rstrip('/')}"
        env = build_runtime_env(proxy_url, session_secret, models)
        if selected and not _is_subcommand(tool_args):
            tool_args = _with_model_arg(tool_args, _selection_id(binary, env, selected, catalog))
        proc = subprocess_cross_os.popen([binary, *tool_args], env=env)
        returncode = _wait_for_exit(proc)
    finally:
        cache.stop()
        server.shutdown()
        server.server_close()
        client.close()
    raise SystemExit(returncode)


def _replace_model_arg(tool_args: list[str], model: str) -> list[str]:
    """``tool_args`` with the last ``--model`` value before ``--`` replaced by ``model``."""
    args = list(tool_args)
    last: int | None = None
    for index, arg in enumerate(args):
        if arg == "--":
            break
        if arg in {"--model", "-m"} and index + 1 < len(args):
            last = index + 1
        elif arg.startswith("--model="):
            last = index
    if last is None:
        return args
    args[last] = f"--model={model}" if args[last].startswith("--model=") else model
    return args


def validate_cmd(binary: str) -> list[str]:
    return [binary, "-p", "say hi in 5 words or less"]
