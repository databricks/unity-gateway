"""Read-only provider catalog requests used by managed discovery evidence."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import NoReturn

_ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"
_CODEX_MODELS_PATH = "/ai-gateway/codex/v1/models"
_PROVIDER_HEADER = "Databricks-Model-Provider-Service"
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


@dataclass(frozen=True)
class CodexProviderCatalog:
    """The list-visible model slugs from a Codex provider catalog."""

    model_ids: tuple[str, ...]


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
        # AI Gateway omits cursor fields but accepts the final model ID as after_id.
        # All model IDs and the nonempty page have already been validated above.
        last_id = models[-1][0]

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
            # Hidden/native aliases are part of the provider response but are not exposed by
            # Codex's model/list request used as the observable catalog below.
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


def _get_json(url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else getattr(response, "status", None)
            if status is not None and not 200 <= status < 300:
                _fail(f"GET {url} returned HTTP {status}")
            raw_body = response.read()
    except urllib.error.HTTPError as error:
        raise AssertionError(f"GET {url} returned HTTP {error.code} {error.reason}") from error
    except (urllib.error.URLError, OSError) as error:
        raise AssertionError(f"GET {url} failed: {error}") from error

    if isinstance(raw_body, bytes):
        try:
            raw_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AssertionError(f"GET {url} returned non-UTF-8 JSON") from error
    if not isinstance(raw_body, str):
        _fail(f"GET {url} returned a non-text response body")
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError as error:
        raise AssertionError(f"GET {url} returned invalid JSON: {error.msg}") from error


def _validate_request_inputs(workspace: str, token: str, provider_service: str) -> str:
    if not isinstance(workspace, str) or not workspace.rstrip("/"):
        _fail("provider catalog request requires a workspace URL")
    if not isinstance(token, str) or not token:
        _fail("provider catalog request requires an explicit bearer token")
    if not isinstance(provider_service, str) or not provider_service.strip():
        _fail("provider catalog request requires a provider service")
    return workspace.rstrip("/")


def fetch_anthropic_provider_catalog(
    workspace: str, token: str, provider_service: str
) -> AnthropicProviderCatalog:
    """Fetch and validate the complete Anthropic provider catalog with bounded pagination."""
    base_url = _validate_request_inputs(workspace, token, provider_service)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Anthropic-Version": _ANTHROPIC_VERSION,
        _PROVIDER_HEADER: provider_service,
    }

    model_ids: list[str] = []
    display_names: dict[str, str | None] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    for _page_number in range(_ANTHROPIC_MAX_PAGES):
        query = {"limit": str(_ANTHROPIC_PAGE_SIZE)}
        if cursor is not None:
            query["after_id"] = cursor
        url = f"{base_url}{_ANTHROPIC_MODELS_PATH}?{urllib.parse.urlencode(query)}"
        page = parse_anthropic_provider_page(_get_json(url, headers))
        for model_id, display_name in page.models:
            if model_id in display_names:
                _fail(f"Anthropic provider catalog repeated model id {model_id!r}")
            model_ids.append(model_id)
            display_names[model_id] = display_name

        if not page.has_more:
            return AnthropicProviderCatalog(tuple(model_ids), display_names)
        assert page.last_id is not None  # checked by parse_anthropic_provider_page
        if page.last_id in cursors:
            _fail(f"Anthropic provider catalog repeated pagination cursor {page.last_id!r}")
        cursors.add(page.last_id)
        cursor = page.last_id

    _fail(f"Anthropic provider catalog exceeded {_ANTHROPIC_MAX_PAGES} pages")


def fetch_codex_provider_catalog(
    workspace: str, token: str, provider_service: str
) -> CodexProviderCatalog:
    """Fetch and validate the list-visible Codex provider catalog."""
    base_url = _validate_request_inputs(workspace, token, provider_service)
    payload = _get_json(
        f"{base_url}{_CODEX_MODELS_PATH}",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            _PROVIDER_HEADER: provider_service,
        },
    )
    return CodexProviderCatalog(parse_codex_provider_catalog(payload))


def fetch_codex_parent_catalog(
    workspace: str, token: str, parent_schema: str
) -> CodexProviderCatalog:
    """Fetch the API-compatible models advertised for a Unity Catalog parent schema."""
    base_url = _validate_request_inputs(workspace, token, parent_schema)
    payload = _get_json(
        f"{base_url}{_CODEX_MODELS_PATH}",
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Databricks-Model-Service-Parent-Schema": parent_schema,
        },
    )
    return CodexProviderCatalog(parse_codex_provider_catalog(payload))
