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

`ug copilot --provider` registers a Model Provider Service's models instead: its Claude models on
the Anthropic endpoint, its other chat models on the gateway's native OpenAI route, with every
registry provider sending the service's routing header. `ug copilot --model-location` registers
the model services of one Unity Catalog schema: those named like Claude or GPT models on their
usual wire, the rest on the Anthropic endpoint like Claude Code, which the gateway translates.

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
    backup_existing_file,
    is_dry_run,
    parse_dotenv,
    read_json_safe,
    write_dotenv,
    write_json_file,
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
    build_copilot_base_url,
    build_tool_base_url,
    list_anthropic_model_catalog,
    list_provider_service_models,
    model_version_sort_key,
    resolve_provider_service,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)
from ucode.smart_routing.routing import unwrap_anthropic_gateway_model
from ucode.state import is_tool_managed, mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_user_agent, ug_version
from ucode.ui import print_warning

from .args import LaunchOptions, explicit_model_arg_value
from .request_tags import request_tags_header_value

COPILOT_CONFIG_DIR = Path.home() / ".copilot"
COPILOT_ENV_PATH = COPILOT_CONFIG_DIR / "ucode.env"
COPILOT_MCP_CONFIG_PATH = COPILOT_CONFIG_DIR / "mcp-config.json"
COPILOT_BACKUP_PATH = APP_DIR / "copilot-ucode-env.backup"
# Older ug versions kept their servers in a side file loaded with `--additional-mcp-config`.
LEGACY_MCP_CONFIG_PATH = COPILOT_CONFIG_DIR / "ucode-mcp-config.json"
LEGACY_MCP_BACKUP_PATH = APP_DIR / "copilot-ucode-mcp-config.backup.json"
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
# The Model Provider Service this launch routes through; set by the launch command, never saved.
LAUNCH_PROVIDER_SERVICE_KEY = "_copilot_launch_provider_service"
# The Unity Catalog schema whose model services this launch uses; never saved.
LAUNCH_MODEL_LOCATION_KEY = "_copilot_launch_model_location"

# The wire APIs Copilot speaks; `responses` and `completions` are Copilot's `wireApi` values.
WIRE_ANTHROPIC = "anthropic"
WIRE_RESPONSES = "responses"
WIRE_COMPLETIONS = "completions"

PROVIDER_NAMES: dict[str, str] = {
    WIRE_ANTHROPIC: "dbx-anthropic",
    WIRE_RESPONSES: "dbx-responses",
    WIRE_COMPLETIONS: "dbx-completions",
}
_WIRE_ROUTES: dict[str, tuple[str, ...]] = {
    WIRE_ANTHROPIC: (API_ANTHROPIC_MESSAGES,),
    WIRE_RESPONSES: (API_MLFLOW_RESPONSES, API_OPENAI_RESPONSES, API_CODEX_RESPONSES),
    WIRE_COMPLETIONS: (API_MLFLOW_CHAT_COMPLETIONS, API_OPENAI_CHAT_COMPLETIONS),
}
_UNSUPPORTED_MODELS = {
    "claude-opus-4-1": "the gateway rejects the thinking settings Copilot sends for it",
}
_GATEWAY_ALIAS_PREFIX = "anthropic-aigw-"
_GPT_MODEL_MAJOR_PATTERN = re.compile(r"^gpt-(\d+)o?(?=$|[.-])")
_CLAUDE_DASHED_VERSION_PATTERN = re.compile(r"-(\d+)-(\d+)$")
# Anthropic and Bedrock Claude ids end in a release date and, on Bedrock, a version:
# `-20250929-v1:0`.
_CLAUDE_RELEASE_SUFFIX_PATTERN = re.compile(r"(?:-\d{8})?(?:-v\d+(?::\d+)?)?$")
_CLAUDE_HAIKU_MAJOR_PATTERN = re.compile(r"^claude-haiku-(\d+)")
# Model Provider Service ids write versions with dots (`gpt-5.6`) or no separator (`o3`); the
# version sort reads dash-separated digits.
_DOTTED_VERSION_PATTERN = re.compile(r"(?<=\d)\.(?=\d)")
_O_SERIES_PATTERN = re.compile(r"^o(?=\d)")
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
_CLAUDE_200K_WINDOW = 200_000

# A Model Provider Service target that declares its APIs is usable only if one is a chat API.
_CHAT_API_TYPES = frozenset(route for routes in _WIRE_ROUTES.values() for route in routes)


def copilot_exclusion_reason(model: str, api_types: Iterable[str] = ()) -> str | None:
    """Why ``model`` can't be offered in Copilot, or None when it can."""
    return classify_model(model, api_types)[1]


def classify_copilot_model(model: str, api_types: Iterable[str] = ()) -> str | None:
    """The wire API (``anthropic``, ``responses`` or ``completions``) Copilot uses for ``model``."""
    return classify_model(model, api_types)[0]


def classify_model(
    model: str, api_types: Iterable[str] = (), *, provider_type: str | None = None
) -> tuple[str | None, str | None]:
    """The wire API Copilot uses for ``model``, or why it can't use it.

    Names decide the wire, so ``api_types`` only excludes: the gateway serves some models on APIs
    they don't declare. Without ``provider_type``, ``model`` is a Databricks model service and
    ``api_types`` its advertised routes. With it, ``model`` is a Model Provider Service target and
    ``api_types`` its declared APIs: Anthropic-API provider types serve only Claude, and other types
    put every model that isn't GPT 5+ or o-series on Chat Completions, except Foundry's Claude
    models, which use Anthropic."""
    declared = tuple(api_types)
    tail = bare_model_name(model)
    if provider_type is None:
        if model.lower().startswith(_GATEWAY_ALIAS_PREFIX):
            return None, "it is a gateway alias, not a model"
        if tail in _UNSUPPORTED_MODELS:
            return None, _UNSUPPORTED_MODELS[tail]
    elif declared and _CHAT_API_TYPES.isdisjoint(declared):
        return None, f"it isn't a chat model (it serves {', '.join(declared)})"
    wire = _wire_from_name(tail)
    if provider_type in ANTHROPIC_API_PROVIDER_TYPES:
        if wire != WIRE_ANTHROPIC:
            return None, "only the Claude models of this service can be used"
        return wire, None
    if provider_type is not None:
        # Foundry serves its Claude targets on the Anthropic route but not on Chat Completions,
        # whatever the targets declare.
        if wire == WIRE_ANTHROPIC and provider_type == MICROSOFT_FOUNDRY_PROVIDER_TYPE:
            return wire, None
        return (WIRE_RESPONSES if wire == WIRE_RESPONSES else WIRE_COMPLETIONS), None
    if wire is None:
        if "gemini-" in tail:
            return None, "the gateway rejects the requests Copilot sends to Gemini models"
        return None, "ug copilot supports Claude and GPT models"
    if declared and not set(declared) & set(_WIRE_ROUTES[wire]):
        return None, f"it isn't served on {_WIRE_ROUTES[wire][0]}"
    return wire, None


def _wire_from_name(name: str) -> str | None:
    if name.startswith("claude-"):
        return WIRE_ANTHROPIC
    gpt = _GPT_MODEL_MAJOR_PATTERN.match(name)
    # Copilot sends `temperature: 0` on Chat Completions, which GPT 5+ and o-series models reject.
    if (gpt and int(gpt.group(1)) >= 5) or _O_SERIES_PATTERN.match(name):
        return WIRE_RESPONSES
    return WIRE_COMPLETIONS if gpt else None


def uses_responses_api(model: str) -> bool:
    """Whether Copilot calls ``model`` through the Responses API: GPT 5+ and o-series models."""
    return _wire_from_name(bare_model_name(model)) == WIRE_RESPONSES


def claude_model_name(model: str) -> str:
    """A Claude model's own name, without its catalog/schema and release suffix:
    ``us.anthropic.claude-sonnet-4-5-20250929-v1:0`` -> ``claude-sonnet-4-5``.

    Other models keep their bare name."""
    name = bare_model_name(model)
    if not name.startswith("claude-"):
        return name
    return _CLAUDE_RELEASE_SUFFIX_PATTERN.sub("", name, count=1)


def copilot_catalog_model_id(model: str) -> str | None:
    """Copilot's catalog id for a Claude model, e.g. ``system.ai.claude-sonnet-5-5`` -> ``claude-sonnet-5.5``.

    Copilot applies Claude request settings to any ``claude-*`` id, so ids missing from its catalog still work.
    Returns None for anything other than Claude.
    """
    tail = claude_model_name(model)
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


def catalog_model_limits(model: CopilotModel, *, external: bool) -> tuple[int, int] | None:
    """:func:`copilot_model_limits` for a Claude ``model`` of the catalog, or None for others.

    ``external`` (a Model Provider Service, or a location's model service) caps them at 200K, which
    such a service may serve instead of the 1M window."""
    limits = copilot_model_limits(model.id) if model.wire == WIRE_ANTHROPIC else None
    if limits is not None and external and sum(limits) > _CLAUDE_200K_WINDOW:
        return _CLAUDE_200K_LIMITS
    return limits


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
            if bare_model_name(model.id).startswith(f"claude-{f}-")
        ),
        len(ANTHROPIC_FAMILIES),
    )
    sort_name = _O_SERIES_PATTERN.sub("o-", _DOTTED_VERSION_PATTERN.sub("-", model.id))
    version_key = model_version_sort_key(sort_name)
    return (tuple(PROVIDER_NAMES).index(model.wire), family, version_key)


def build_catalog(
    models: Iterable[tuple[str, Iterable[str]]],
    *,
    provider_type: str | None = None,
    fallback_wire: str | None = None,
) -> tuple[list[CopilotModel], list[tuple[str, str]]]:
    """The ``(model, api_types)`` pairs Copilot can use, sorted Claude first (by family, newest
    first) then GPT, and the skipped models with the reason (see :func:`classify_model`).

    With ``fallback_wire``, a model whose name doesn't decide the wire uses it instead of being
    skipped; unsupported models are still skipped."""
    usable: dict[str, CopilotModel] = {}
    skipped: dict[str, str] = {}
    for model, api_types in models:
        wire, reason = classify_model(model, api_types, provider_type=provider_type)
        if wire is None and fallback_wire and bare_model_name(model) not in _UNSUPPORTED_MODELS:
            wire = fallback_wire
        if wire is not None:
            usable.setdefault(model, CopilotModel(model, wire))
        elif reason is not None:
            skipped.setdefault(model, reason)
    return sorted(usable.values(), key=_catalog_sort_key), list(skipped.items())


def build_copilot_catalog(entries: Iterable[ModelServiceEntry | str]) -> list[CopilotModel]:
    """The Databricks models Copilot can use (see :func:`build_catalog`)."""
    pairs = ((e, ()) if isinstance(e, str) else (e.id, e.api_types) for e in entries)
    return build_catalog(pairs)[0]


def _without_registry_prefix(model: str) -> str:
    """``model`` without a Copilot registry provider prefix: ``dbx-responses/gpt-5.5`` -> ``gpt-5.5``."""
    name, separator, rest = model.partition("/")
    return rest if separator and name in PROVIDER_NAMES.values() else model


def provider_service_catalog(
    state: dict, service_name: str, requested: str | None, *, tool: str = "copilot"
) -> list[CopilotModel]:
    """The models of Model Provider Service ``service_name`` that this launch registers.

    A service that allows every model registers what the gateway lists for it, plus ``requested``
    (the launch's ``--model``) or the managed default, so a model the listing omits can be named.
    ``tool`` is ``vscode`` for Copilot Chat in VS Code, which has neither."""
    workspace = state["workspace"]
    token = launch_token(state, workspace)
    service, error = resolve_provider_service(tool, service_name, workspace, token)
    if error or service is None:
        raise RuntimeError(error or f"Model provider service '{service_name}' was not found.")
    agent = "Copilot" if tool == "copilot" else "VS Code"
    provider_type = service["provider_type"]
    named = list(service.get("targets") or [])
    listed: list[str] = []
    allow_all = bool(service.get("allow_all_targets"))
    if allow_all:
        start = _without_registry_prefix(requested) if requested else None
        managed_default = state.get("copilot_default_model") if tool == "copilot" else None
        start = start or (managed_default if isinstance(managed_default, str) else None)
        listed, reason = list_provider_service_models(workspace, token, service_name, provider_type)
        if reason and not start:
            raise RuntimeError(
                f"Could not list the models of '{service_name}': {reason}."
                + (
                    " Name the one to start on with `ug copilot --model <id>`."
                    if tool == "copilot"
                    else ""
                )
            )
        if reason:
            print_warning(f"Could not list the models of {service_name}: {reason}.")
        if start:
            named.append(start)
    api_types = service.get("target_api_types") or {}
    catalog, skipped = build_catalog(
        ((model, api_types.get(model, ())) for model in [*named, *listed]),
        provider_type=provider_type,
    )
    # An allow-all listing includes models Copilot can't use; report only declared or named ones.
    skipped = [(model, reason) for model, reason in skipped if model in named]
    if not catalog:
        detail = "".join(f" {model} ({reason})." for model, reason in skipped)
        raise RuntimeError(
            f"Model provider service '{service_name}' has no models {agent} can use.{detail}"
            + (
                " Name one it serves with `ug copilot --model <id>`."
                if allow_all and tool == "copilot"
                else ""
            )
        )
    if skipped:
        print_warning(
            f"Skipping models of {service_name} that {agent} can't use: "
            + "; ".join(f"{model} ({reason})" for model, reason in skipped)
            + "."
        )
    return catalog


def location_catalog(state: dict, schema: str) -> list[CopilotModel]:
    """The model services of Unity Catalog schema ``schema`` that this launch registers.

    The listing names some services by gateway alias (``anthropic-aigw-<hash>-<name>``); Copilot
    sends the full name, which the gateway routes directly."""
    workspace = state["workspace"]
    listing = list_anthropic_model_catalog(
        workspace, launch_token(state, workspace), parent_schema=schema
    )
    models = (unwrap_anthropic_gateway_model(model) for model in listing.model_ids)
    catalog, _ = build_catalog(((model, ()) for model in models), fallback_wire=WIRE_ANTHROPIC)
    if not catalog:
        reason = f": {listing.error_msg}" if listing.error_msg else ""
        raise RuntimeError(f"Found no model services to use in {schema}{reason}.")
    return catalog


def catalog_from_listings(
    entries: Iterable[ModelServiceEntry] | None, fallback: Iterable[str]
) -> list[CopilotModel] | None:
    """The Copilot catalog from this run's model listings.

    ``entries`` is the UC model-services listing (None when it failed); ``fallback`` holds the ids
    the per-family gateway listings found instead. None when neither answered, so the caller keeps
    the saved catalog; an empty catalog when they answered with nothing Copilot can use."""
    listed = list(entries or ())
    fallback_catalog = build_copilot_catalog(list(fallback))
    if not listed:
        return fallback_catalog or None
    catalog = build_copilot_catalog(listed)
    # A family the UC listing lacks keeps the models its gateway listing found.
    families = {model.wire == WIRE_ANTHROPIC for model in catalog}
    missing = [
        model for model in fallback_catalog if (model.wire == WIRE_ANTHROPIC) not in families
    ]
    return sorted([*catalog, *missing], key=_catalog_sort_key)


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
    state: dict,
    catalog: list[CopilotModel],
    *,
    requested: str | None = None,
    warn: bool = True,
    managed_list: bool = True,
) -> CopilotModel:
    """The model a session starts on: ``requested`` (budget recommendation or managed default),
    then the managed default and list, then the newest Sonnet, Opus or Haiku, then GPT.

    ``managed_list=False`` ignores the managed model lists, which name Databricks models."""
    static = _string_list(state.get("copilot_static_models")) if managed_list else []
    managed_models = _string_list(state.get("copilot_models")) if managed_list else []
    candidates = [
        requested,
        state.get("copilot_default_model"),
        next((m for m in static if classify_copilot_model(m)), None),
        managed_models[0] if managed_models else None,
    ]
    chosen = _first_available(dict.fromkeys(c for c in candidates if c), catalog, warn=warn)
    if chosen is not None:
        return chosen
    for prefix in ("claude-sonnet-", "claude-opus-", "claude-haiku-"):
        family = next((m for m in catalog if bare_model_name(m.id).startswith(prefix)), None)
        if family is not None:
            return family
    return next((m for m in catalog if m.wire != WIRE_ANTHROPIC), catalog[0])


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
    catalog: list[CopilotModel],
    proxy_base_url: str,
    session_secret: str,
    *,
    provider_service: str | None = None,
    model_location: str | None = None,
) -> dict:
    """Copilot's providers.json for ``catalog``: one provider per wire, all on the session proxy.

    A provider with no models makes Copilot probe ``GET <baseUrl>/models``, so only used wires are
    listed. With ``provider_service`` (a Model Provider Service), every registry provider carries its
    routing header and OpenAI wires use the gateway's native OpenAI route, which serves Model
    Provider Services untranslated."""
    providers = []
    for wire, name in PROVIDER_NAMES.items():
        if not any(model.wire == wire for model in catalog):
            continue
        provider: dict[str, Any]
        if wire == WIRE_ANTHROPIC:
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
                "baseUrl": (
                    f"{proxy_base_url}/ai-gateway/openai/v1"
                    if provider_service
                    else build_copilot_base_url(proxy_base_url)
                ),
            }
        # `bearerToken` reaches the proxy as `Authorization` on every provider type.
        provider["bearerToken"] = session_secret
        if provider_service:
            provider["headers"] = {MODEL_PROVIDER_SERVICE_HEADER: provider_service}
        providers.append(provider)
    models = []
    for model in catalog:
        entry: dict[str, Any] = {
            "id": model.id,
            "provider": model.provider,
            "wireModel": model.id,
            "name": model.id,
        }
        catalog_id = copilot_catalog_model_id(model.id) if model.wire == WIRE_ANTHROPIC else None
        if catalog_id is not None:
            entry["modelId"] = catalog_id
        limits = catalog_model_limits(model, external=bool(provider_service or model_location))
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
        # Under the mutex, so a concurrent sweep can't reopen the lock file before it's deleted
        # (Windows refuses to delete an open file).
        with _registries_lock():
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


def copilot_mcp_config_path() -> Path:
    """The user config Copilot reads ``mcpServers`` from (and ``copilot mcp add`` writes):
    ``$COPILOT_HOME/mcp-config.json`` when that env var is set, else ``~/.copilot/mcp-config.json``."""
    copilot_home = os.environ.get("COPILOT_HOME")
    return Path(copilot_home) / "mcp-config.json" if copilot_home else COPILOT_MCP_CONFIG_PATH


def _read_mcp_config_for_rewrite(path: Path) -> dict:
    """``path`` parsed for a rewrite (``{}`` when absent). Raises rather than return ``{}`` for a
    file that isn't a JSON object, since rewriting it would drop the developer's own servers."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Can't update {path}: it isn't a valid JSON object. Fix it, then retry."
        )
    return data


def remove_legacy_mcp_config() -> bool:
    """Delete the legacy side file and its backup (on revert: everything in them is ug's)."""
    existed = LEGACY_MCP_CONFIG_PATH.exists() or LEGACY_MCP_BACKUP_PATH.exists()
    if is_dry_run():
        return existed
    LEGACY_MCP_CONFIG_PATH.unlink(missing_ok=True)
    LEGACY_MCP_BACKUP_PATH.unlink(missing_ok=True)
    return existed


def _take_legacy_mcp_servers() -> dict[str, dict]:
    """The servers in the legacy side file, deleting it and its backup. Every entry there was
    written by ug, so it moves into the user config as-is."""
    servers = read_json_safe(LEGACY_MCP_CONFIG_PATH).get("mcpServers")
    remove_legacy_mcp_config()
    if not isinstance(servers, dict):
        return {}
    return {name: entry for name, entry in servers.items() if isinstance(entry, dict)}


def _apply_mcp_diff(add: dict[str, dict], remove: set[str]) -> set[str]:
    path = copilot_mcp_config_path()
    config = _read_mcp_config_for_rewrite(path)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    legacy = _take_legacy_mcp_servers()
    removed = {name for name in remove if name in servers or name in legacy}
    if not add and not removed and not legacy:
        return removed
    servers.update({name: entry for name, entry in legacy.items() if name not in remove})
    for name in remove:
        servers.pop(name, None)
    servers.update(add)
    config["mcpServers"] = servers
    write_json_file(path, config)
    return removed


def migrate_legacy_mcp_config() -> None:
    """Move the servers in the legacy side file into the user config, once."""
    if LEGACY_MCP_CONFIG_PATH.exists() or LEGACY_MCP_BACKUP_PATH.exists():
        _apply_mcp_diff({}, set())


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    """Add (or replace) a stdio (`ug mcp-proxy`) MCP server in Copilot's user config. Returns
    True when an entry of that name was replaced."""
    return bool(_apply_mcp_diff({name: build_mcp_server_entry(argv)}, {name}))


def remove_mcp_server_config(name: str) -> bool:
    return bool(_apply_mcp_diff({}, {name}))


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Copilot's user-config ``mcpServers`` in a single
    read-modify-write. Returns the names actually removed."""
    return _apply_mcp_diff(add, remove)


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


def write_tool_config(state: dict, model: str | None) -> dict:
    scrub_stored_credentials()
    if not is_tool_managed(state, "copilot"):
        backup_existing_file(COPILOT_ENV_PATH, COPILOT_BACKUP_PATH)
    existing = parse_dotenv(COPILOT_ENV_PATH)
    for key in LEGACY_ENV_KEYS:
        existing.pop(key, None)
    if model is None:
        existing.pop("COPILOT_MODEL", None)
    else:
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
    headers = {
        "User-Agent": ucode_user_agent(ug_version(), f"copilot/{agent_version(SPEC['binary'])}")
    }
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


def _session_catalog(
    state: dict, tool_args: list[str], provider_service: str | None, model_location: str | None
) -> list[CopilotModel]:
    """The models this launch registers: the Model Provider Service's, the model location's,
    else the Databricks catalog."""
    if provider_service:
        return provider_service_catalog(
            state, provider_service, explicit_model_arg_value(tool_args)
        )
    if model_location:
        return location_catalog(state, model_location)
    catalog = copilot_catalog(state)
    if not catalog:
        raise RuntimeError(
            "No Copilot model is available on this workspace: ug copilot supports its Claude "
            "and GPT models. Run `ug configure --agents copilot` to rediscover them."
        )
    for model, reason in unservable_static_models(state):
        print_warning(f"Skipping {model} from your workspace's managed config: {reason}.")
    return catalog


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    provider_service = state.pop(LAUNCH_PROVIDER_SERVICE_KEY, None)
    model_location = state.pop(LAUNCH_MODEL_LOCATION_KEY, None)
    catalog = _session_catalog(state, tool_args, provider_service, model_location)
    # Resolve before the proxy starts so a bad model fails without starting Copilot.
    tool_args, selected = rewrite_model_args(tool_args, catalog)
    start_model = selected or launch_model(
        state,
        catalog,
        requested=options.launch_model,
        managed_list=not (provider_service or model_location),
    )
    scrub_stored_credentials()
    try:
        migrate_legacy_mcp_config()
    except RuntimeError as exc:
        print_warning(f"ug's MCP servers aren't available in this session: {exc}")

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
        # Copilot sends `reasoning_effort` on Chat Completions, but only models that aren't GPT 5+
        # or o-series use that wire, and OpenAI and Azure reject the field for them.
        dropped_chat_completions_fields=("reasoning_effort",),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        proxy_base_url = f"http://{LOOPBACK_HOST}:{server.server_address[1]}"
        config = render_providers_config(
            catalog,
            proxy_base_url,
            session_secret,
            provider_service=provider_service,
            model_location=model_location,
        )
        wire_override = os.environ.get("COPILOT_PROVIDER_WIRE_MODEL")
        if wire_override:
            for entry in config["models"]:
                if entry["id"] == start_model.id:
                    entry["wireModel"] = wire_override
        with session_registry(config) as registry_path:
            env = build_runtime_env(registry_path, start_model)
            proc = subprocess_cross_os.popen(
                [SPEC["binary"], *MCP_LAUNCH_ARGS, *tool_args], env=env
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
        *MCP_LAUNCH_ARGS,
        "--prompt",
        "say hi in 5 words or less",
        "--allow-all-tools",
    ]


# Offline mode empties Copilot's allowlist of servers whose `instructions` reach the model, so
# without this the gateway's servers lose theirs.
MCP_LAUNCH_ARGS = ("--allow-all-mcp-server-instructions",)
