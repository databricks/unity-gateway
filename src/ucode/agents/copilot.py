"""GitHub Copilot CLI agent: runs Copilot against a per-session Databricks model registry.

Copilot reads its BYOK models from a providers.json registry (`COPILOT_PROVIDERS_CONFIG`). Each
`ug copilot` launch writes one (~/.copilot/ug-providers-<id>.json) listing every Databricks
model Copilot can use,
so `/model` switches between them mid-session, across wire APIs. Each family keeps its native
wire: Claude on the gateway's Anthropic endpoint (with Copilot's catalog id as `modelId`, so
Copilot sends Claude request settings), GPT 5+ on Responses (Copilot sends `temperature: 0` on Chat
Completions, which those models reject), older GPT on Chat Completions. Gemini and open-source
models are excluded: Databricks' Gemini translation rejects the `stream_options` field Copilot
sends, and open-source models are not validated with Copilot yet.

Copilot holds its provider credential for the whole session, so every provider points at a
per-session loopback proxy (`gateway_proxy.start_gateway_auth_proxy`): Copilot authenticates to
the proxy with a random session secret, written only into the session registry, and the proxy
forwards each request with a fresh Databricks token. ~/.copilot/ucode.env is written for
inspection and `revert`; Copilot never reads it and it never holds a credential.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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
from ucode.custom_oauth import launch_token
from ucode.databricks import (
    ANTHROPIC_FAMILIES,
    ModelServiceEntry,
    build_copilot_base_url,
    build_tool_base_url,
    model_version_sort_key,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.state import is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ug_version
from ucode.ui import print_warning

from .args import LaunchOptions
from .request_tags import request_tags_header_value

COPILOT_CONFIG_DIR = Path.home() / ".copilot"
COPILOT_ENV_PATH = COPILOT_CONFIG_DIR / "ucode.env"
COPILOT_MCP_CONFIG_PATH = COPILOT_CONFIG_DIR / "ucode-mcp-config.json"
COPILOT_BACKUP_PATH = APP_DIR / "copilot-ucode-env.backup"
COPILOT_MCP_BACKUP_PATH = APP_DIR / "copilot-ucode-mcp-config.backup.json"
COPILOT_PROXY_LOG_PATH = APP_DIR / "logs" / "copilot-proxy.log"
_REGISTRY_PREFIX = "ug-providers-"
_REGISTRY_LOCK_NAME = "ug-providers.lock"

MINIMUM_COPILOT_VERSION = (1, 0, 91)

SPEC: ToolSpec = {
    "binary": "copilot",
    "package": "@github/copilot",
    "display": "GitHub Copilot CLI",
    "config_path": COPILOT_ENV_PATH,
    "backup_path": COPILOT_BACKUP_PATH,
}

MANAGED_KEYS: list[str] = ["COPILOT_MODEL", "COPILOT_OFFLINE"]
LEGACY_ENV_KEYS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "COPILOT_PROVIDER_API_KEY",
    # Single-provider settings earlier versions wrote; the session registry replaces them.
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_WIRE_API",
    "COPILOT_PROVIDER_MODEL_ID",
]
# Earlier versions wrote the Databricks token (or PAT) under these keys.
CREDENTIAL_ENV_KEYS = ("COPILOT_PROVIDER_BEARER_TOKEN", "OAUTH_TOKEN")
CATALOG_STATE_KEY = "copilot_catalog"

PROVIDER_NAMES: dict[str, str] = {
    "anthropic": "dbx-anthropic",
    "responses": "dbx-responses",
    "completions": "dbx-completions",
}
_WIRE_ROUTES: dict[str, tuple[str, ...]] = {
    "anthropic": ("anthropic/v1/messages",),
    "responses": ("mlflow/v1/responses", "openai/v1/responses", "codex/v1/responses"),
    "completions": ("mlflow/v1/chat/completions", "openai/v1/chat/completions"),
}
_UNSUPPORTED_MODELS = {
    "claude-opus-4-1": "the gateway rejects the thinking settings Copilot sends for it",
}
_GATEWAY_ALIAS_PREFIX = "anthropic-aigw-"
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^gpt-(\d+)(?=$|[.-])")
_CLAUDE_DASHED_VERSION_PATTERN = re.compile(r"-(\d+)-(\d+)$")
_CLAUDE_HAIKU_MAJOR_PATTERN = re.compile(r"^claude-haiku-(\d+)")
_CLAUDE_CATALOG_VERSION_PATTERN = re.compile(r"^claude-([a-z]+)-(\d+)(?:\.(\d+))?$")

# (maxPromptTokens, maxOutputTokens) by Copilot catalog id. Copilot assumes a 128K prompt for
# registry models without limits, and its own values differ between versions.
_CLAUDE_LIMITS: dict[str, tuple[int, int]] = {
    "claude-fable-5.1": (936_000, 32_000),
    "claude-fable-5": (936_000, 32_000),
    "claude-opus-5.5": (936_000, 32_000),
    "claude-opus-5": (936_000, 32_000),
    "claude-opus-4.8": (936_000, 32_000),
    "claude-opus-4.7": (936_000, 32_000),
    "claude-opus-4.6": (936_000, 32_000),
    "claude-opus-4.5": (168_000, 32_000),
    "claude-sonnet-5.5": (872_000, 128_000),
    "claude-sonnet-5": (936_000, 32_000),
    "claude-sonnet-4.6": (936_000, 32_000),
    "claude-sonnet-4.5": (168_000, 32_000),
    "claude-haiku-4.5": (136_000, 64_000),
}
_CLAUDE_1M_LIMITS = (936_000, 32_000)
_CLAUDE_200K_LIMITS = (168_000, 32_000)


def _model_tail(model: str) -> str:
    """The model name without its ``<catalog>.<schema>.`` and ``databricks-`` prefixes or ``[...]`` suffix."""
    name = model.split("[", 1)[0].lower()
    tail = name.split(".", 2)[-1] if name.count(".") >= 2 else name
    return tail.removeprefix("databricks-")


def copilot_exclusion_reason(model: str, api_types: Iterable[str] = ()) -> str | None:
    """Why ``model`` can't be offered in Copilot, or None when it can."""
    return _classify(model, tuple(api_types))[1]


def classify_copilot_model(model: str, api_types: Iterable[str] = ()) -> str | None:
    """The wire API (``anthropic``, ``responses`` or ``completions``) Copilot uses for ``model``.

    Names decide the wire; ``api_types`` (the model service's advertised routes) only excludes,
    because the gateway serves some models on routes they don't list."""
    return _classify(model, tuple(api_types))[0]


def _classify(model: str, api_types: tuple[str, ...]) -> tuple[str | None, str | None]:
    if model.lower().startswith(_GATEWAY_ALIAS_PREFIX):
        return None, "it is a gateway alias, not a model"
    tail = _model_tail(model)
    if tail in _UNSUPPORTED_MODELS:
        return None, _UNSUPPORTED_MODELS[tail]
    gpt = _GPT_MODEL_MAJOR_PATTERN.match(tail)
    if tail.startswith("claude-"):
        wire = "anthropic"
    elif gpt:
        wire = "responses" if int(gpt.group(1)) >= 5 else "completions"
    elif "gemini-" in tail:
        return None, "the gateway rejects the requests Copilot sends to Gemini models"
    else:
        return None, "ug copilot supports Claude and GPT models"
    if api_types and not set(api_types) & set(_WIRE_ROUTES[wire]):
        return None, f"it isn't served on {_WIRE_ROUTES[wire][0]}"
    return wire, None


def copilot_catalog_model_id(model: str) -> str | None:
    """Copilot's catalog id for a Claude model, e.g. ``system.ai.claude-sonnet-5-5`` -> ``claude-sonnet-5.5``.

    Copilot applies Claude request settings to any ``claude-*`` id, so ids missing from its catalog still work.
    Returns None for anything other than Claude.
    """
    tail = _model_tail(model)
    if not tail.startswith("claude-"):
        return None
    catalog_id = _CLAUDE_DASHED_VERSION_PATTERN.sub(r"-\1.\2", tail)
    # Copilot sends budget thinking for any Haiku it doesn't know, which Haiku 5+ rejects; Sonnet
    # ids get the adaptive thinking those models require.
    haiku = _CLAUDE_HAIKU_MAJOR_PATTERN.match(catalog_id)
    if haiku and int(haiku.group(1)) >= 5:
        catalog_id = catalog_id.replace("claude-haiku-", "claude-sonnet-", 1)
    return catalog_id


def copilot_model_limits(model: str) -> tuple[int, int] | None:
    """``(maxPromptTokens, maxOutputTokens)`` for a Claude model, or None for other models."""
    catalog_id = copilot_catalog_model_id(model)
    if catalog_id is None:
        return None
    if catalog_id in _CLAUDE_LIMITS:
        return _CLAUDE_LIMITS[catalog_id]
    version = _CLAUDE_CATALOG_VERSION_PATTERN.match(catalog_id)
    if version is None:
        return _CLAUDE_200K_LIMITS
    family, major, minor = version.group(1), int(version.group(2)), int(version.group(3) or 0)
    one_million = major >= 5 or (family in ("opus", "sonnet") and (major, minor) >= (4, 6))
    return _CLAUDE_1M_LIMITS if one_million else _CLAUDE_200K_LIMITS


@dataclass(frozen=True)
class CopilotModel:
    """A Databricks model in Copilot's registry and the wire API it is served on."""

    id: str
    wire: str

    @property
    def provider(self) -> str:
        return PROVIDER_NAMES[self.wire]

    @property
    def ref(self) -> str:
        """Copilot's selection id; a bare model id isn't matched against the registry."""
        return f"{self.provider}/{self.id}"


def _catalog_sort_key(model: CopilotModel) -> tuple:
    family = next(
        (
            i
            for i, f in enumerate(ANTHROPIC_FAMILIES)
            if _model_tail(model.id).startswith(f"claude-{f}-")
        ),
        len(ANTHROPIC_FAMILIES),
    )
    return (tuple(PROVIDER_NAMES).index(model.wire), family, model_version_sort_key(model.id))


def build_copilot_catalog(entries: Iterable[ModelServiceEntry | str]) -> list[CopilotModel]:
    """The models Copilot can use, Claude first (by family, newest first), then GPT."""
    models: dict[str, CopilotModel] = {}
    for entry in entries:
        model, api_types = (entry, ()) if isinstance(entry, str) else (entry.id, entry.api_types)
        wire = classify_copilot_model(model, api_types)
        if wire is not None and model not in models:
            models[model] = CopilotModel(model, wire)
    return sorted(models.values(), key=_catalog_sort_key)


def catalog_from_listings(
    entries: Iterable[ModelServiceEntry] | None, fallback: Iterable[str]
) -> list[CopilotModel] | None:
    """The Copilot catalog from this run's model listings.

    ``entries`` is the UC model-services listing (None when it failed); ``fallback`` holds the ids
    the per-family gateway listings found instead. None when neither answered, so the caller keeps
    the saved catalog; an empty catalog when they answered with nothing Copilot can use."""
    listed = list(entries or ())
    if listed:
        return build_copilot_catalog(listed)
    fallback_ids = list(fallback)
    return build_copilot_catalog(fallback_ids) if fallback_ids else None


def catalog_state(models: list[CopilotModel]) -> dict:
    return {
        "models": [model.id for model in models],
        "discovered_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _string_list(value: object) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def copilot_catalog(state: dict) -> list[CopilotModel]:
    """The models this launch registers: the managed list, else the discovered catalog.

    State saved before the catalog existed falls back to the Claude and GPT discovery lists."""
    static = build_copilot_catalog(_string_list(state.get("copilot_static_models")))
    if static:
        return static
    saved = state.get(CATALOG_STATE_KEY)
    if isinstance(saved, dict):
        return build_copilot_catalog(_string_list(saved.get("models")))
    claude_models = state.get("claude_models")
    legacy = list(claude_models.values()) if isinstance(claude_models, dict) else []
    return build_copilot_catalog([*_string_list(legacy), *_string_list(state.get("codex_models"))])


def unservable_static_models(state: dict) -> list[tuple[str, str]]:
    """Managed models Copilot can't use, with the reason, so the launch can say why they're missing.

    Empty when none of the list is usable: the launch then reports that the whole list was
    ignored instead."""
    static = _string_list(state.get("copilot_static_models"))
    if not build_copilot_catalog(static):
        return []
    skipped = []
    for model in static:
        reason = copilot_exclusion_reason(model)
        if reason is not None:
            skipped.append((model, reason))
    return skipped


def _normalized(model: str) -> str:
    return model.split("[", 1)[0].lower()


def resolve_copilot_model(requested: str, catalog: list[CopilotModel]) -> CopilotModel:
    """Match ``requested`` (an id, a short name or ``provider/id``) to a catalog model.

    Raises RuntimeError listing the available models when nothing matches."""
    for matches in (
        lambda m: m.id == requested,
        lambda m: m.ref == requested,
        lambda m: _normalized(m.id) == _normalized(requested),
        lambda m: _normalized(m.id) == f"system.ai.{_normalized(requested)}",
        lambda m: _normalized(m.id) == f"databricks-{_normalized(requested)}",
    ):
        found = [model for model in catalog if matches(model)]
        if found:
            return next((m for m in found if m.id.startswith("system.ai.")), found[0])
    reason = copilot_exclusion_reason(requested)
    detail = f" ({reason})" if reason else ""
    available = "\n".join(
        f"  {provider}: {', '.join(m.id for m in catalog if m.provider == provider)}"
        for provider in PROVIDER_NAMES.values()
        if any(m.provider == provider for m in catalog)
    )
    raise RuntimeError(
        f"Model '{requested}' isn't available in ug copilot{detail}. Available models:\n{available}"
    )


def _first_available(
    candidates: Iterable[str | None], catalog: list[CopilotModel], *, warn: bool
) -> CopilotModel | None:
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return resolve_copilot_model(candidate, catalog)
        except RuntimeError:
            if warn:
                print_warning(
                    f"{candidate} isn't available in ug copilot; starting on another model."
                )
    return None


def launch_model(
    state: dict, catalog: list[CopilotModel], *, requested: str | None = None, warn: bool = True
) -> CopilotModel:
    """The model a session starts on: ``requested`` (budget recommendation or managed default),
    then the managed default and list, then the newest Sonnet, Opus or Haiku, then GPT."""
    servable_static = (
        m for m in _string_list(state.get("copilot_static_models")) if classify_copilot_model(m)
    )
    managed_models = _string_list(state.get("copilot_models"))
    candidates = [
        requested,
        state.get("copilot_default_model"),
        next(servable_static, None),
        managed_models[0] if managed_models else None,
    ]
    chosen = _first_available(dict.fromkeys(c for c in candidates if c), catalog, warn=warn)
    if chosen is not None:
        return chosen
    for prefix in ("claude-sonnet-", "claude-opus-", "claude-haiku-"):
        family = next((m for m in catalog if _model_tail(m.id).startswith(prefix)), None)
        if family is not None:
            return family
    return next((m for m in catalog if m.wire != "anthropic"), catalog[0])


def default_model(state: dict) -> str | None:
    catalog = copilot_catalog(state)
    return launch_model(state, catalog, warn=False).id if catalog else None


def rewrite_model_args(
    tool_args: list[str], catalog: list[CopilotModel]
) -> tuple[list[str], CopilotModel | None]:
    """Resolve every ``--model``/``-m`` before ``--`` to Copilot's ``provider/id`` form.

    Copilot has no ``-m`` alias, so it becomes ``--model``. Returns the rewritten args and the
    last model selected, which is the one Copilot uses."""
    rewritten: list[str] = []
    selected: CopilotModel | None = None
    index = 0
    while index < len(tool_args):
        arg = tool_args[index]
        if arg == "--":
            rewritten.extend(tool_args[index:])
            break
        value: str | None = None
        if (
            arg in {"--model", "-m"}
            and index + 1 < len(tool_args)
            and not tool_args[index + 1].startswith("-")
        ):
            value = tool_args[index + 1]
            index += 1
        elif arg.startswith("--model=") and arg.partition("=")[2]:
            value = arg.partition("=")[2]
        if value is None:
            rewritten.append(arg)
        else:
            selected = resolve_copilot_model(value, catalog)
            rewritten.extend(["--model", selected.ref])
        index += 1
    return rewritten, selected


def render_providers_config(
    catalog: list[CopilotModel], proxy_base_url: str, session_secret: str
) -> dict:
    """Copilot's providers.json for ``catalog``: one provider per wire, all on the session proxy.

    A provider with no models makes Copilot probe ``GET <baseUrl>/models``, so only used wires are
    listed."""
    providers = []
    for wire, name in PROVIDER_NAMES.items():
        if not any(model.wire == wire for model in catalog):
            continue
        if wire == "anthropic":
            provider = {
                "name": name,
                "type": "anthropic",
                "baseUrl": build_tool_base_url("claude", proxy_base_url),
            }
        else:
            provider = {
                "name": name,
                "type": "openai",
                "wireApi": wire,
                "baseUrl": build_copilot_base_url(proxy_base_url),
            }
        # `bearerToken` reaches the proxy as `Authorization` on every provider type.
        provider["bearerToken"] = session_secret
        providers.append(provider)
    models = []
    for model in catalog:
        entry: dict[str, Any] = {
            "id": model.id,
            "provider": model.provider,
            "wireModel": model.id,
            "name": model.id,
        }
        catalog_id = copilot_catalog_model_id(model.id) if model.wire == "anthropic" else None
        if catalog_id is not None:
            entry["modelId"] = catalog_id
        limits = copilot_model_limits(model.id) if model.wire == "anthropic" else None
        if limits is not None:
            entry["maxPromptTokens"], entry["maxOutputTokens"] = limits
        models.append(entry)
    return {"providers": providers, "models": models}


def build_runtime_env(registry_path: Path, model: CopilotModel) -> dict[str, str]:
    """Copilot's launch environment: the session registry and the model to start on.

    Inherited ``COPILOT_PROVIDER*`` settings are dropped so a user's own BYOK setup can't take
    over a ug session."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("COPILOT_PROVIDER")
    }
    env["COPILOT_PROVIDERS_CONFIG"] = str(registry_path)
    env["COPILOT_MODEL"] = model.ref
    # Fail closed without the registry instead of falling back to GitHub sign-in.
    env["COPILOT_OFFLINE"] = "true"
    return env


@contextmanager
def _registries_lock() -> Iterator[None]:
    COPILOT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with (COPILOT_CONFIG_DIR / _REGISTRY_LOCK_NAME).open("a+b") as lock_file:
        acquire_exclusive_file_lock(lock_file)
        try:
            yield
        finally:
            release_file_lock(lock_file)


def _sweep_dead_registries() -> None:
    for registry in COPILOT_CONFIG_DIR.glob(f"{_REGISTRY_PREFIX}*.json"):
        lock_path = registry.with_suffix(".lock")
        if lock_path.exists():
            with lock_path.open("a+b") as lock_file:
                try:
                    acquire_exclusive_file_lock(lock_file, timeout=0)
                except (TimeoutError, OSError):
                    continue
                release_file_lock(lock_file)
        registry.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def sweep_session_registries() -> None:
    """Delete registries left by sessions that ended without cleaning up (e.g. a killed terminal)."""
    if not COPILOT_CONFIG_DIR.exists():
        return
    with _registries_lock():
        _sweep_dead_registries()


@contextmanager
def session_registry(config: dict) -> Iterator[Path]:
    """Write ``config`` to a private per-session providers.json, removed when the session ends.

    The session holds a lock on a sibling ``.lock`` file so a concurrent sweep leaves it alone;
    locking the registry itself would block Copilot from reading it on Windows."""
    with _registries_lock():
        _sweep_dead_registries()
        path = COPILOT_CONFIG_DIR / f"{_REGISTRY_PREFIX}{secrets.token_hex(8)}.json"
        lock_path = path.with_suffix(".lock")
        lock_file = lock_path.open("a+b")
        try:
            acquire_exclusive_file_lock(lock_file, timeout=0)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as registry:
                json.dump(config, registry, indent=2)
        except BaseException:
            lock_file.close()
            path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
            raise
    try:
        yield path
    finally:
        release_file_lock(lock_file)
        lock_file.close()
        path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def minimum_version_error() -> str | None:
    installed = agent_version(SPEC["binary"])
    parsed = _parse_version(installed)
    if parsed is None or parsed >= MINIMUM_COPILOT_VERSION:
        return None
    required = ".".join(str(part) for part in MINIMUM_COPILOT_VERSION)
    return (
        f"GitHub Copilot CLI {installed} is too old. ug requires {required} or newer for "
        f"Databricks model switching. Update it with `npm install -g {SPEC['package']}`."
    )


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
    for key in LEGACY_ENV_KEYS:
        existing.pop(key, None)
    try:
        existing["COPILOT_MODEL"] = resolve_copilot_model(model, copilot_catalog(state)).ref
    except RuntimeError:
        existing["COPILOT_MODEL"] = model
    existing["COPILOT_OFFLINE"] = "true"
    write_dotenv(COPILOT_ENV_PATH, existing)
    state = mark_tool_managed(state, "copilot", MANAGED_KEYS)
    save_state(state)
    return state


def _token_provider(state: dict) -> Callable[[bool], str]:
    workspace = state["workspace"]
    return lambda force_refresh: launch_token(state, workspace, force_refresh=force_refresh)


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
    catalog = copilot_catalog(state)
    if not catalog:
        raise RuntimeError(
            "No Copilot model is available on this workspace: ug copilot supports its Claude and "
            "GPT models. Run `ug configure --agents copilot` to rediscover them."
        )
    for model, reason in unservable_static_models(state):
        print_warning(f"Skipping {model} from your workspace's managed config: {reason}.")
    # Resolve before the proxy starts so a bad model fails without starting Copilot.
    tool_args, selected = rewrite_model_args(tool_args, catalog)
    start_model = selected or launch_model(state, catalog, requested=options.launch_model)
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
        config = render_providers_config(catalog, proxy_base_url, session_secret)
        wire_override = os.environ.get("COPILOT_PROVIDER_WIRE_MODEL")
        if wire_override:
            for entry in config["models"]:
                if entry["id"] == start_model.id:
                    entry["wireModel"] = wire_override
        with session_registry(config) as registry_path:
            env = build_runtime_env(registry_path, start_model)
            proc = subprocess_cross_os.popen(
                [SPEC["binary"], *mcp_config_args(), *tool_args], env=env
            )
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
