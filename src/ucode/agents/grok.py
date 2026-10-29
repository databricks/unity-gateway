"""Grok Build agent: runs xAI's Grok Build CLI against Databricks models.

Grok Build reads custom models from `[model.<id>]` tables in `$GROK_HOME/config.toml`, each naming
its wire API (`api_backend`), `base_url`, and the environment variable holding its API key
(`env_key`). `ug grok` runs Grok with a private GROK_HOME (~/.ucode/grok-home) whose config lists
the Databricks models Grok can use, each on the gateway wire it works on: Claude on Anthropic
Messages, GPT on Responses, and Grok and open-source models on Chat Completions. Gemini models are excluded: the
gateway's Gemini translation rejects the JSON Schema keywords (`$schema`, `type` arrays) in Grok's
tool definitions on every wire.

Grok holds a model's key for the whole session. Its own renewable credential, the
`[auth] auth_provider_command` login, is also sent to xAI's servers (model list, settings,
feedback), so ug never hands Grok a Databricks token. Instead every model's `base_url` is
`${UG_GROK_PROXY_URL}/...` and its `env_key` names `UG_GROK_SESSION_SECRET`: `ug grok` sets both
for a per-session loopback proxy (`gateway_proxy.start_gateway_auth_proxy`) that accepts the secret
and forwards each request with a fresh Databricks token. Grok expands the variables when it loads
the config, so the file holds no per-session value and concurrent sessions each reach their own
proxy.

`ug grok --provider` registers a Model Provider Service's models instead; the proxy adds the
service's routing header.

Every launch writes its own models into the shared config, which Grok reads once at startup. Grok
has no per-session config (with `GROK_CONFIG_PATH` set, 1.0.67 lists no models from that file and
starts on xAI's default), so a launch rewrites its models under a lock and holds it until Grok has
loaded them; a concurrent launch waits rather than replacing them first.

The private home keeps a user's own Grok login, settings, and sessions apart from ug's. Grok still
discovers skills in ~/.agents/skills and ~/.claude/skills, where `ug skills` downloads them.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

import tomlkit
from tomlkit.items import Table

from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    read_toml_safe,
    write_toml_file,
)
from ucode.constants import (
    AI_GATEWAY_REQUEST_TAGS_HEADER,
    API_ANTHROPIC_MESSAGES,
    API_CODEX_RESPONSES,
    API_MLFLOW_CHAT_COMPLETIONS,
    API_MLFLOW_RESPONSES,
    API_OPENAI_CHAT_COMPLETIONS,
    API_OPENAI_RESPONSES,
    LOOPBACK_HOST,
    MODEL_PROVIDER_SERVICE_HEADER,
)
from ucode.custom_oauth import launch_token
from ucode.databricks import (
    ANTHROPIC_API_PROVIDER_TYPES,
    ANTHROPIC_FAMILIES,
    MICROSOFT_FOUNDRY_PROVIDER_TYPE,
    ModelServiceEntry,
    bare_model_name,
    classify_model_family,
    list_provider_service_models,
    model_token_limits,
    model_version_sort_key,
    resolve_provider_service,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.state import is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_user_agent, ug_version
from ucode.ui import print_warning

from . import proxy_launch
from .args import LaunchOptions
from .request_tags import request_tags_header_value

GROK_HOME_DIR = APP_DIR / "grok-home"
GROK_CONFIG_PATH = GROK_HOME_DIR / "config.toml"
GROK_BACKUP_PATH = APP_DIR / "grok-config.backup.toml"
GROK_PROXY_LOG_PATH = APP_DIR / "logs" / "grok-proxy.log"

SPEC: ToolSpec = {
    "binary": "grok",
    "package": "@xai-official/grok",
    "display": "Grok Build",
    "config_path": GROK_CONFIG_PATH,
    "backup_path": GROK_BACKUP_PATH,
}

# Set for each session; the config refers to them as `${...}` so it never holds a session value.
PROXY_URL_ENV = "UG_GROK_PROXY_URL"
SESSION_SECRET_ENV = "UG_GROK_SESSION_SECRET"
# Inherited settings that would take a ug session off the gateway: an xAI key or login command
# reaches xAI's models, and the model overrides replace ug's catalog or default.
_DROPPED_ENV = (
    "XAI_API_KEY",
    "GROK_AUTH_PROVIDER_COMMAND",
    "GROK_DEFAULT_MODEL",
    "GROK_MODELS_BASE_URL",
    "GROK_MODELS_LIST_URL",
)
# The Model Provider Service this launch routes through; set by the launch command, never saved.
LAUNCH_PROVIDER_SERVICE_KEY = "_grok_launch_provider_service"
# The `system.ai` models ug grok offers, from the model-services listing (which, unlike the
# per-family discovery lists, keeps every Claude version and the Grok models).
CATALOG_STATE_KEY = "grok_catalog"

MANAGED_KEYS: list[list[str]] = [["model"], ["models"]]

# Grok's `api_backend` values.
WIRE_MESSAGES = "messages"
WIRE_RESPONSES = "responses"
WIRE_CHAT_COMPLETIONS = "chat_completions"

# Gateway routes, relative to the proxy. Grok appends `/messages`, `/responses`, or
# `/chat/completions`. A Model Provider Service's OpenAI-wire models use the native OpenAI route,
# which serves them untranslated.
_ANTHROPIC_ROUTE = "/ai-gateway/anthropic/v1"
_MLFLOW_ROUTE = "/ai-gateway/mlflow/v1"
_OPENAI_ROUTE = "/ai-gateway/openai/v1"

# A Model Provider Service target that declares its APIs is usable only if one is a chat API.
_CHAT_API_TYPES = frozenset(
    {
        API_ANTHROPIC_MESSAGES,
        API_MLFLOW_RESPONSES,
        API_OPENAI_RESPONSES,
        API_CODEX_RESPONSES,
        API_MLFLOW_CHAT_COMPLETIONS,
        API_OPENAI_CHAT_COMPLETIONS,
    }
)
_GPT_MAJOR_PATTERN = re.compile(r"^gpt-(\d+)")
_O_SERIES_PATTERN = re.compile(r"^o\d")
_CLAUDE_MAX_OUTPUT_TOKENS = 32_000
# How long a launch holds the config lock waiting for Grok to load its models.
_STARTUP_TIMEOUT_SECONDS = 15.0

# The models and start model this process's last `write_tool_config` wrote; `launch` starts Grok on
# exactly these, whatever another launch has written since.
_written: tuple[list[GrokModel], GrokModel] | None = None


@dataclass(frozen=True)
class GrokModel:
    """A model in Grok's catalog, the wire Grok speaks to it on, and its gateway route."""

    id: str
    wire: str
    route: str


def classify_databricks_model(model: str) -> GrokModel | None:
    """How Grok reaches Databricks model service ``model``, or None when ug grok doesn't offer it.

    Claude uses Anthropic Messages and GPT uses Responses. Grok, gpt-oss and the supported
    open-source families use Chat Completions: Grok models reject the `reasoning.summary` Grok
    sends on Responses, and gpt-oss replies there lack the usage details Grok requires. Gemini
    (see the module docstring) and other families are not offered."""
    name = bare_model_name(model)
    family = classify_model_family(name)
    if family in ANTHROPIC_FAMILIES:
        return GrokModel(model, WIRE_MESSAGES, _ANTHROPIC_ROUTE)
    if name.startswith("grok-") or name.startswith("gpt-oss") or family == "oss":
        return GrokModel(model, WIRE_CHAT_COMPLETIONS, _MLFLOW_ROUTE)
    if family == "codex":
        return GrokModel(model, WIRE_RESPONSES, _MLFLOW_ROUTE)
    return None


def _catalog_sort_key(model: GrokModel) -> tuple:
    """Grok, Claude by family, GPT, then open source; newest first within each group."""
    name = bare_model_name(model.id)
    family = classify_model_family(name)
    if name.startswith("grok-"):
        group: tuple = (0,)
    elif family in ANTHROPIC_FAMILIES:
        group = (1, ANTHROPIC_FAMILIES.index(family))
    elif family == "codex" and not name.startswith("gpt-oss"):
        group = (2,)
    else:
        group = (3,)
    return (*group, model_version_sort_key(name))


def catalog_from_listing(entries: Iterable[ModelServiceEntry]) -> list[str]:
    """The chat models in a `system.ai` model-services listing that ug grok offers, in order."""
    catalog = [
        entry
        for item in entries
        if (not item.api_types or not _CHAT_API_TYPES.isdisjoint(item.api_types))
        and (entry := classify_databricks_model(item.id)) is not None
    ]
    return [model.id for model in sorted(catalog, key=_catalog_sort_key)]


def databricks_catalog(state: dict) -> list[GrokModel]:
    """The workspace's models Grok can use: those of the model-services listing `ug configure`
    saved, else the per-family discovery lists (when the listing failed or offered none)."""
    catalog = _classified(_string_list(state.get(CATALOG_STATE_KEY)))
    if not catalog:
        claude_models = state.get("claude_models")
        claude = list(claude_models.values()) if isinstance(claude_models, dict) else []
        catalog = _classified(
            [
                *_string_list(claude),
                *_string_list(state.get("codex_models")),
                *_string_list(state.get("oss_models")),
            ]
        )
    return sorted(catalog, key=_catalog_sort_key)


def _classified(models: list[str]) -> list[GrokModel]:
    catalog = [entry for model in models if (entry := classify_databricks_model(model))]
    return list({model.id: model for model in catalog}.values())


def _string_list(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def classify_provider_target(
    model: str, provider_type: str, api_types: Iterable[str] = ()
) -> tuple[GrokModel | None, str | None]:
    """How Grok reaches Model Provider Service target ``model``, or why it can't.

    Anthropic-API provider types serve only Claude, on the Anthropic route. Other types serve Claude
    there too (Foundry), GPT 5+ and o-series models on Responses, and the rest on Chat Completions,
    all on the native OpenAI route."""
    declared = tuple(api_types)
    if declared and _CHAT_API_TYPES.isdisjoint(declared):
        return None, f"it isn't a chat model (it serves {', '.join(declared)})"
    name = bare_model_name(model)
    if name.startswith("claude-"):
        if (
            provider_type in ANTHROPIC_API_PROVIDER_TYPES
            or provider_type == MICROSOFT_FOUNDRY_PROVIDER_TYPE
        ):
            return GrokModel(model, WIRE_MESSAGES, _ANTHROPIC_ROUTE), None
    elif provider_type in ANTHROPIC_API_PROVIDER_TYPES:
        return None, "ug grok uses only the Claude models of this service"
    if "gemini-" in name:
        return None, "the gateway rejects the tool schemas Grok sends to Gemini models"
    gpt = _GPT_MAJOR_PATTERN.match(name)
    if (gpt and int(gpt.group(1)) >= 5) or _O_SERIES_PATTERN.match(name):
        return GrokModel(model, WIRE_RESPONSES, _OPENAI_ROUTE), None
    return GrokModel(model, WIRE_CHAT_COMPLETIONS, _OPENAI_ROUTE), None


def provider_service_catalog(
    state: dict, service_name: str, requested: str | None
) -> list[GrokModel]:
    """The models of Model Provider Service ``service_name`` that this launch registers.

    A service that allows every model registers what the gateway lists for it, plus ``requested``
    (the launch's model) so a model the listing omits can be named."""
    workspace = state["workspace"]
    token = launch_token(state, workspace)
    service, error = resolve_provider_service("grok", service_name, workspace, token)
    if error or service is None:
        raise RuntimeError(error or f"Model provider service '{service_name}' was not found.")
    provider_type = service["provider_type"]
    named = _string_list(service.get("targets"))
    listed: list[str] = []
    allow_all = bool(service.get("allow_all_targets"))
    if allow_all:
        listed, reason = list_provider_service_models(workspace, token, service_name, provider_type)
        if reason and not requested:
            raise RuntimeError(
                f"Could not list the models of '{service_name}': {reason}. Name the one to start "
                "on with `ug grok --model <id>`."
            )
        if reason:
            print_warning(f"Could not list the models of {service_name}: {reason}.")
        if requested:
            named.append(requested)
    api_types = service.get("target_api_types") or {}
    catalog: dict[str, GrokModel] = {}
    skipped: dict[str, str] = {}
    for model in [*named, *listed]:
        entry, reason = classify_provider_target(model, provider_type, api_types.get(model, ()))
        if entry is not None:
            catalog.setdefault(model, entry)
        elif reason is not None and model in named:
            # An allow-all listing includes models Grok can't use; report only named ones.
            skipped.setdefault(model, reason)
    if not catalog:
        detail = "".join(f" {model} ({reason})." for model, reason in skipped.items())
        raise RuntimeError(
            f"Model provider service '{service_name}' has no models ug grok can use.{detail}"
            + (" Name one it serves with `ug grok --model <id>`." if allow_all else "")
        )
    if skipped:
        print_warning(
            f"Skipping models of {service_name} that ug grok can't use: "
            + "; ".join(f"{model} ({reason})" for model, reason in skipped.items())
            + "."
        )
    return list(catalog.values())


def _normalized(model: str) -> str:
    return model.split("[", 1)[0].lower()


def resolve_model(requested: str, catalog: list[GrokModel]) -> GrokModel:
    """Match ``requested`` (a model id or its short name) to a catalog model.

    Raises RuntimeError listing the available models when nothing matches."""
    requested = requested.strip()
    wanted = _normalized(requested)
    for matches in (
        lambda m: m.id == requested,
        lambda m: _normalized(m.id) == wanted,
        lambda m: _normalized(m.id) == f"system.ai.{wanted}",
        lambda m: _normalized(m.id) == f"databricks-{wanted}",
    ):
        found = [model for model in catalog if matches(model)]
        if found:
            return found[0]
    if "gemini-" in bare_model_name(requested):
        detail = " (the gateway rejects the tool schemas Grok sends to Gemini models)"
    else:
        detail = ""
    available = ", ".join(model.id for model in catalog) or "none"
    raise RuntimeError(
        f"Model '{requested}' isn't available in ug grok{detail}. Available models: {available}"
    )


def launch_model(catalog: list[GrokModel], requested: str | None = None) -> GrokModel:
    """The model a session starts on: ``requested``, else the newest Grok, then Opus, Sonnet or
    Haiku, else the first model listed."""
    if requested:
        return resolve_model(requested, catalog)
    grok = next((m for m in catalog if bare_model_name(m.id).startswith("grok-")), None)
    if grok is not None:
        return grok
    for family in ("opus", "sonnet", "haiku"):
        found = next(
            (m for m in catalog if bare_model_name(m.id).startswith(f"claude-{family}-")), None
        )
        if found is not None:
            return found
    return catalog[0]


def _summary_model(catalog: list[GrokModel], default: GrokModel) -> GrokModel:
    """The model Grok titles and summarizes sessions with: a Haiku when listed, else the default.

    Grok otherwise names one of xAI's models, which isn't served here."""
    haiku = next((m for m in catalog if bare_model_name(m.id).startswith("claude-haiku-")), None)
    return haiku or default


def default_model(state: dict) -> str | None:
    catalog = databricks_catalog(state)
    return launch_model(catalog).id if catalog else None


def _model_table(model: GrokModel) -> Table:
    table = tomlkit.table()
    table["model"] = model.id
    table["name"] = model.id
    table["base_url"] = f"${{{PROXY_URL_ENV}}}{model.route}"
    table["env_key"] = SESSION_SECRET_ENV
    table["api_backend"] = model.wire
    limits = model_token_limits(model.id)
    if limits is not None:
        table["context_window"] = limits["context"]
        table["max_completion_tokens"] = limits["output"]
    elif model.wire == WIRE_MESSAGES:
        # Grok asks custom Messages models for 128K output tokens, which older Claude models
        # reject (Haiku 4.5 allows 64K, Opus 4.1 32K); every Claude model accepts 32K.
        table["max_completion_tokens"] = _CLAUDE_MAX_OUTPUT_TOKENS
    return table


def render_config(
    doc: tomlkit.TOMLDocument, catalog: list[GrokModel], default: GrokModel
) -> tomlkit.TOMLDocument:
    """Replace ug's models in ``doc``, keeping every other setting (e.g. what `/settings` saves).

    ug owns the `[model.*]` tables that read its session secret, and in `[models]` the default,
    summary, and image models and `allowed_models`, which keeps xAI's own models (unreachable
    without an xAI login) out of the picker and `-m`."""
    models_super = doc.get("model")
    if not isinstance(models_super, dict):
        models_super = tomlkit.table(is_super_table=True)
        doc["model"] = models_super
    for name in [
        name
        for name, entry in models_super.items()
        if isinstance(entry, dict) and entry.get("env_key") == SESSION_SECRET_ENV
    ]:
        del models_super[name]
    for model in catalog:
        models_super[model.id] = _model_table(model)

    selection = doc.get("models")
    if not isinstance(selection, dict):
        selection = tomlkit.table()
        doc["models"] = selection
    selection["default"] = default.id
    summary = _summary_model(catalog, default).id
    selection["session_summary"] = summary
    selection["image_description"] = default.id
    selection["allowed_models"] = [model.id for model in catalog]
    return doc


def _session_catalog(state: dict, provider: str | None, requested: str | None) -> list[GrokModel]:
    if provider:
        return provider_service_catalog(state, provider, requested)
    catalog = databricks_catalog(state)
    if not catalog:
        raise RuntimeError(
            "No Grok Build model is available on this workspace: ug grok supports its Grok, Claude, "
            "GPT, and open-source models. Run `ug configure --agents grok` to rediscover them."
        )
    return catalog


@contextmanager
def _config_lock() -> Iterator[None]:
    """Serialize ug's writes to Grok's config, and each launch's write through Grok's startup."""
    lock_path = GROK_CONFIG_PATH.with_name(".ug-config.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        acquire_exclusive_file_lock(lock_file)
        try:
            yield
        finally:
            release_file_lock(lock_file)


def _write_models(catalog: list[GrokModel], start: GrokModel) -> None:
    write_toml_file(
        GROK_CONFIG_PATH, render_config(read_toml_safe(GROK_CONFIG_PATH), catalog, start)
    )


def write_tool_config(state: dict, model: str | None, *, provider: str | None = None) -> dict:
    """Write ug's models into Grok's private config, starting sessions on ``model`` when given."""
    global _written
    catalog = _session_catalog(state, provider, model)
    start = launch_model(catalog, model)
    if not is_tool_managed(state, "grok"):
        backup_existing_file(GROK_CONFIG_PATH, GROK_BACKUP_PATH)
    with _config_lock():
        _write_models(catalog, start)
    _written = (catalog, start)
    state = mark_tool_managed(state, "grok", MANAGED_KEYS)
    save_state(state)
    return state


def configured_paths(state: dict) -> list[str]:
    return [str(GROK_CONFIG_PATH)]


def build_mcp_server_entry(argv: list[str]) -> dict:
    # A stdio `[mcp_servers.<name>]` entry. ug registers the `ug mcp-proxy ...` bridge here so
    # Grok never speaks HTTP+bearer directly; the proxy refreshes the token itself.
    return {"command": argv[0], "args": list(argv[1:])}


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to `[mcp_servers]` in Grok's private config in a single
    read-modify-write. Returns the names actually removed."""
    with _config_lock():
        doc = read_toml_safe(GROK_CONFIG_PATH)
        servers = doc.get("mcp_servers")
        removed = {name for name in remove if isinstance(servers, dict) and name in servers}
        if not add and not removed:
            return removed
        if not isinstance(servers, dict):
            servers = tomlkit.table(is_super_table=True)
            doc["mcp_servers"] = servers
        for name in removed:
            del servers[name]
        for name, entry in add.items():
            if name in servers:
                del servers[name]
            table = tomlkit.table()
            table.update(entry)
            servers[name] = table
        write_toml_file(GROK_CONFIG_PATH, doc)
    return removed


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    """Register one MCP server; True when it replaced an existing entry."""
    doc = read_toml_safe(GROK_CONFIG_PATH)
    servers = doc.get("mcp_servers")
    existed = isinstance(servers, dict) and name in servers
    write_user_mcp_servers({name: build_mcp_server_entry(argv)}, set())
    return existed


def remove_mcp_server_config(name: str) -> bool:
    return name in write_user_mcp_servers({}, {name})


def mcp_cli_env() -> dict[str, str]:
    """The environment for `grok mcp ...` commands, reading ug's private home."""
    env = os.environ.copy()
    env["GROK_HOME"] = str(GROK_HOME_DIR)
    return env


def build_runtime_env(proxy_base_url: str, session_secret: str) -> dict[str, str]:
    """Grok's launch environment: ug's private home and this session's proxy and secret."""
    env = {key: value for key, value in os.environ.items() if key not in _DROPPED_ENV}
    env["GROK_HOME"] = str(GROK_HOME_DIR)
    env[PROXY_URL_ENV] = proxy_base_url
    env[SESSION_SECRET_ENV] = session_secret
    return env


def rewrite_model_args(tool_args: list[str], catalog: list[GrokModel]) -> list[str]:
    """Resolve every ``--model``/``-m`` before ``--`` to its catalog id, so short names work."""
    rewritten: list[str] = []
    index = 0
    while index < len(tool_args):
        arg = tool_args[index]
        if arg == "--":
            rewritten.extend(tool_args[index:])
            break
        if (
            arg in {"--model", "-m"}
            and index + 1 < len(tool_args)
            and not tool_args[index + 1].startswith("-")
        ):
            rewritten.extend([arg, resolve_model(tool_args[index + 1], catalog).id])
            index += 2
            continue
        if arg.startswith("--model=") and arg.partition("=")[2]:
            rewritten.append(f"--model={resolve_model(arg.partition('=')[2], catalog).id}")
        else:
            rewritten.append(arg)
        index += 1
    return rewritten


def proxy_headers(provider: str | None) -> dict[str, str]:
    """Headers the proxy adds to every gateway request."""
    headers = {
        "User-Agent": ucode_user_agent(ug_version(), f"grok/{agent_version(SPEC['binary'])}")
    }
    request_tags = request_tags_header_value()
    if request_tags:
        headers[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    if provider:
        headers[MODEL_PROVIDER_SERVICE_HEADER] = provider
    return headers


def _open_sessions() -> set[str]:
    """The ids of the sessions Grok lists as open; its TUI adds one on startup."""
    try:
        sessions = json.loads(
            GROK_CONFIG_PATH.with_name("active_sessions.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return set()
    if not isinstance(sessions, list):
        return set()
    return {
        str(entry["session_id"])
        for entry in sessions
        if isinstance(entry, dict) and entry.get("session_id")
    }


class _StartingProcess(proxy_launch.ChildProcess, Protocol):
    def poll(self) -> int | None: ...


def _wait_until_started(
    proc: _StartingProcess, requested: threading.Event, before: set[str]
) -> None:
    """Return once Grok has loaded its config: the TUI opens a session (any new one is this
    launch's, since the lock holds other launches back), a headless run sends its first model
    request, and a subcommand exits. Bounded, so an unrecognized startup only delays a concurrent
    launch."""
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if requested.wait(0.05) or proc.poll() is not None or _open_sessions() - before:
            return


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch Grok on the models `configure_tool` just wrote, behind this session's proxy."""
    provider = state.pop(LAUNCH_PROVIDER_SERVICE_KEY, None)
    if _written is None:
        state = write_tool_config(state, options.user_pinned_model, provider=provider)
    assert _written is not None
    catalog, start = _written
    # Resolve before the proxy starts so a bad model fails without starting Grok.
    tool_args = rewrite_model_args(tool_args, catalog)

    # Only Grok launches need the proxy (and its `httpx` import).
    from ucode import gateway_proxy

    workspace = state["workspace"]
    session_secret = secrets.token_urlsafe(32)
    requested = threading.Event()

    def note_request(_path: str, body: bytes) -> bytes:
        # A request means Grok has loaded its models; the body is forwarded unchanged.
        requested.set()
        return body

    server, cache, client = gateway_proxy.start_gateway_auth_proxy(
        workspace,
        proxy_launch.token_provider(state),
        session_secret=session_secret,
        added_headers=proxy_headers(provider),
        sign_in_hint=f"Run `databricks auth login --host {workspace}`, then restart `ug grok`.",
        on_refresh_failure=proxy_launch.refresh_failure_logger(GROK_PROXY_LOG_PATH),
        # On Chat Completions the gateway can't route requests whose tool schemas declare
        # `$schema`, as all of Grok's do, and rejects the backend `model_id` Grok echoes back in
        # earlier assistant messages.
        dropped_message_fields=("model_id",),
        dropped_tool_parameter_keys=("$schema",),
        body_rewriter=note_request,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = build_runtime_env(
            f"http://{LOOPBACK_HOST}:{server.server_address[1]}", session_secret
        )
        with _config_lock():
            # Another launch may have replaced the models since configure; Grok reads them once.
            _write_models(catalog, start)
            before = _open_sessions()
            proc = subprocess_cross_os.popen([SPEC["binary"], *tool_args], env=env)
            _wait_until_started(proc, requested, before)
        returncode = proxy_launch.wait_for_exit(proc)
    finally:
        cache.stop()
        server.shutdown()
        server.server_close()
        client.close()
    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--no-auto-update", "-p", "say hi in 5 words or less"]
