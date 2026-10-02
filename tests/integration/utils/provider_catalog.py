"""Read-only provider catalog requests used by managed discovery evidence."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import NoReturn

from .http import safe_https_json_get

_ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"
_CODEX_MODELS_PATH = "/ai-gateway/codex/v1/models"
_PROVIDER_HEADER = "Databricks-Model-Provider-Service"
MODEL_SERVICE_PARENT_SCHEMA_HEADER = "Databricks-Model-Service-Parent-Schema"
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_PAGE_SIZE = 1000
_ANTHROPIC_MAX_PAGES = 20


@dataclass(frozen=True)
class AnthropicProviderPage:
    """One page returned by the Anthropic-compatible models endpoint."""

    models: tuple[tuple[str, str | None], ...]
    has_more: bool
    last_id: str | None


@dataclass(frozen=True)
class AnthropicProviderCatalog:
    """The complete provider catalog and its optional picker labels."""

    model_ids: tuple[str, ...]
    display_names: dict[str, str | None]
    payloads: tuple[dict, ...] = ()


@dataclass(frozen=True)
class CodexProviderCatalog:
    """The list-visible model slugs from a Codex provider catalog."""

    model_ids: tuple[str, ...]
    payloads: tuple[dict, ...] = ()


def _fail(message: str) -> NoReturn:
    raise AssertionError(message)


def parse_anthropic_provider_page(payload: object) -> AnthropicProviderPage:
    """Validate and extract one Anthropic models response page.

    The provider response is deliberately validated more strictly than the product's discovery
    path: a malformed response must not be mistaken for evidence that the application saw the
    expected catalog.
    """
    if not isinstance(payload, dict):
        _fail(f"Anthropic provider catalog page was not an object: {payload!r}")

    raw_models = payload.get("data")
    if not isinstance(raw_models, list) or not raw_models:
        _fail(f"Anthropic provider catalog page had no model data: {payload!r}")

    models: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for index, raw_model in enumerate(raw_models):
        if not isinstance(raw_model, dict):
            _fail(f"Anthropic provider model {index} was not an object: {raw_model!r}")
        model_id = raw_model.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            _fail(f"Anthropic provider model {index} had an invalid id: {raw_model!r}")
        if model_id in seen:
            _fail(f"Anthropic provider catalog repeated model id {model_id!r}")
        seen.add(model_id)

        display_name = raw_model.get("display_name")
        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            _fail(f"Anthropic provider model {model_id!r} had an invalid display name")
        models.append((model_id, display_name))

    has_more = payload.get("has_more")
    if not isinstance(has_more, bool):
        _fail(f"Anthropic provider catalog page had an invalid has_more value: {payload!r}")

    last_id = payload.get("last_id")
    if last_id is not None and (not isinstance(last_id, str) or not last_id.strip()):
        _fail(f"Anthropic provider catalog page had an invalid last_id: {payload!r}")
    if has_more and last_id is None:
        _fail("Anthropic provider catalog page requested another page without last_id")

    return AnthropicProviderPage(tuple(models), has_more, last_id)


def parse_codex_provider_catalog(payload: object) -> tuple[str, ...]:
    """Validate a Codex catalog and return only models visible in the list API."""
    if not isinstance(payload, dict):
        _fail(f"Codex provider catalog was not an object: {payload!r}")

    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        _fail(f"Codex provider catalog had no models: {payload!r}")

    model_ids: list[str] = []
    seen: set[str] = set()
    for index, raw_model in enumerate(raw_models):
        if not isinstance(raw_model, dict):
            _fail(f"Codex provider model {index} was not an object: {raw_model!r}")
        visibility = raw_model.get("visibility")
        if not isinstance(visibility, str):
            _fail(f"Codex provider model {index} had an invalid visibility")
        if visibility != "list":
            continue
        slug = raw_model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            _fail(f"Codex provider model {index} had an invalid list-visible slug")
        if slug in seen:
            _fail(f"Codex provider catalog repeated list-visible slug {slug!r}")
        seen.add(slug)
        model_ids.append(slug)

    if not model_ids:
        _fail("Codex provider catalog had no list-visible models")
    return tuple(model_ids)


def _validate_scope(scope: str) -> None:
    if not isinstance(scope, str) or not scope.strip():
        _fail("provider catalog request requires an explicit catalog scope")


def fetch_anthropic_provider_catalog(
    workspace: str, token: str, provider_service: str
) -> AnthropicProviderCatalog:
    """Fetch and validate the complete Anthropic provider catalog with bounded pagination."""
    _validate_scope(provider_service)
    return _fetch_anthropic_catalog(workspace, token, {_PROVIDER_HEADER: provider_service})


def fetch_anthropic_parent_catalog(
    workspace: str, token: str, parent_schema: str
) -> AnthropicProviderCatalog:
    """Fetch the complete Anthropic catalog scoped to a Unity Catalog parent schema."""
    _validate_scope(parent_schema)
    return _fetch_anthropic_catalog(
        workspace, token, {MODEL_SERVICE_PARENT_SCHEMA_HEADER: parent_schema}
    )


def _fetch_anthropic_catalog(
    workspace: str, token: str, scope_headers: dict[str, str]
) -> AnthropicProviderCatalog:
    headers = {"Anthropic-Version": _ANTHROPIC_VERSION, **scope_headers}

    model_ids: list[str] = []
    display_names: dict[str, str | None] = {}
    payloads: list[dict] = []
    cursor: str | None = None
    cursors: set[str] = set()
    for _page_number in range(_ANTHROPIC_MAX_PAGES):
        query = {"limit": str(_ANTHROPIC_PAGE_SIZE)}
        if cursor is not None:
            query["after_id"] = cursor
        path = f"{_ANTHROPIC_MODELS_PATH}?{urllib.parse.urlencode(query)}"
        payload = safe_https_json_get(workspace, token, path, headers=headers)
        page = parse_anthropic_provider_page(payload)
        assert isinstance(payload, dict), "Expected an Anthropic catalog object"
        payloads.append(payload)
        for model_id, display_name in page.models:
            if model_id in display_names:
                _fail(f"Anthropic provider catalog repeated model id {model_id!r}")
            model_ids.append(model_id)
            display_names[model_id] = display_name

        if not page.has_more:
            return AnthropicProviderCatalog(tuple(model_ids), display_names, tuple(payloads))
        assert page.last_id is not None
        if page.last_id in cursors:
            _fail(f"Anthropic provider catalog repeated pagination cursor {page.last_id!r}")
        cursors.add(page.last_id)
        cursor = page.last_id

    _fail(f"Anthropic provider catalog exceeded {_ANTHROPIC_MAX_PAGES} pages")


def fetch_codex_provider_catalog(
    workspace: str, token: str, provider_service: str
) -> CodexProviderCatalog:
    """Fetch and validate the list-visible Codex provider catalog."""
    _validate_scope(provider_service)
    return _fetch_codex_catalog(workspace, token, {_PROVIDER_HEADER: provider_service})


def _fetch_codex_catalog(
    workspace: str, token: str, scope_headers: dict[str, str]
) -> CodexProviderCatalog:
    payload = safe_https_json_get(workspace, token, _CODEX_MODELS_PATH, headers=scope_headers)
    model_ids = parse_codex_provider_catalog(payload)
    assert isinstance(payload, dict), "Expected a Codex catalog object"
    return CodexProviderCatalog(model_ids, (payload,))


def fetch_codex_parent_catalog(
    workspace: str, token: str, parent_schema: str
) -> CodexProviderCatalog:
    """Fetch the API-compatible models advertised for a Unity Catalog parent schema."""
    _validate_scope(parent_schema)
    return _fetch_codex_catalog(
        workspace, token, {MODEL_SERVICE_PARENT_SCHEMA_HEADER: parent_schema}
    )
