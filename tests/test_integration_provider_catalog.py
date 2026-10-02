"""Offline checks for the independent managed-provider metadata oracle."""

import io
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from tests.integration.utils import provider_catalog as catalog


def _mock_json_boundary(monkeypatch, get_json):
    class CatalogOpener:
        def open(self, request, *, timeout):
            assert request.get_method() == "GET"
            assert timeout == 30
            names = (
                "Authorization",
                "Accept",
                "Anthropic-Version",
                "Databricks-Model-Provider-Service",
                catalog.MODEL_SERVICE_PARENT_SCHEMA_HEADER,
            )
            headers = {
                name: request.get_header(name.capitalize())
                for name in names
                if request.has_header(name.capitalize())
            }
            return io.BytesIO(json.dumps(get_json(request.full_url, headers)).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", CatalogOpener().open)


def _page(*ids, has_more=False, last_id=None):
    return {
        "data": [{"id": model_id, "display_name": model_id} for model_id in ids],
        "has_more": has_more,
        "last_id": last_id,
    }


def test_anthropic_provider_page_preserves_ids_and_labels():
    page = catalog.parse_anthropic_provider_page(_page("claude-a", "claude-b"))
    assert page.models == (("claude-a", "claude-a"), ("claude-b", "claude-b"))
    assert page.has_more is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": [], "has_more": False},
        {"data": [None], "has_more": False},
        {"data": [{"id": ""}], "has_more": False},
        {"data": [{"id": 7}], "has_more": False},
        {"data": [{"id": "a", "display_name": 7}], "has_more": False},
        _page("a", "a"),
        _page("a", has_more="false"),
        _page("a", has_more=True),
        _page("a", has_more=True, last_id=""),
    ],
)
def test_anthropic_provider_page_rejects_invalid_evidence(payload):
    with pytest.raises(AssertionError):
        catalog.parse_anthropic_provider_page(payload)


def test_codex_provider_catalog_selects_only_list_visible_models():
    payload = {
        "models": [
            {"slug": "gpt-a", "visibility": "list"},
            {"slug": "gpt-hidden", "visibility": "hide"},
            {"slug": "gpt-b", "visibility": "list"},
        ]
    }
    assert catalog.parse_codex_provider_catalog(payload) == ("gpt-a", "gpt-b")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"models": []},
        {"models": [None]},
        {"models": [{"slug": "gpt-a"}]},
        {"models": [{"slug": "gpt-a", "visibility": "hide"}]},
        {"models": [{"slug": "", "visibility": "list"}]},
        {"models": [{"slug": 7, "visibility": "list"}]},
        {"models": [{"slug": "gpt-a", "visibility": "list"}] * 2},
    ],
)
def test_codex_provider_catalog_rejects_invalid_evidence(payload):
    with pytest.raises(AssertionError):
        catalog.parse_codex_provider_catalog(payload)


def test_anthropic_provider_fetch_paginates_and_rejects_cross_page_duplicates(monkeypatch):
    requests = []
    pages = iter([_page("a", has_more=True, last_id="a"), _page("b")])

    def get_json(url, headers):
        requests.append((url, headers))
        return next(pages)

    _mock_json_boundary(monkeypatch, get_json)
    result = catalog.fetch_anthropic_provider_catalog("https://workspace/", "token", "c.s.mps")
    assert result.model_ids == ("a", "b")
    assert result.display_names == {"a": "a", "b": "b"}
    assert urlparse(requests[0][0]).path == "/ai-gateway/anthropic/v1/models"
    assert "after_id" not in parse_qs(urlparse(requests[0][0]).query)
    assert parse_qs(urlparse(requests[1][0]).query)["after_id"] == ["a"]
    for _, headers in requests:
        assert headers["Authorization"] == "Bearer token"
        assert headers["Databricks-Model-Provider-Service"] == "c.s.mps"
        assert headers["Anthropic-Version"] == "2023-06-01"
        assert catalog.MODEL_SERVICE_PARENT_SCHEMA_HEADER not in headers

    pages = iter([_page("a", has_more=True, last_id="a"), _page("a")])
    with pytest.raises(AssertionError, match="repeated model id"):
        catalog.fetch_anthropic_provider_catalog("https://workspace", "token", "c.s.mps")


def test_anthropic_provider_fetch_rejects_repeated_cursor(monkeypatch):
    pages = iter(
        [_page("a", has_more=True, last_id="cursor"), _page("b", has_more=True, last_id="cursor")]
    )
    _mock_json_boundary(monkeypatch, lambda *args: next(pages))
    with pytest.raises(AssertionError, match="repeated pagination cursor"):
        catalog.fetch_anthropic_provider_catalog("https://workspace", "token", "c.s.mps")


def test_anthropic_provider_fetch_has_a_page_bound(monkeypatch):
    pages = iter(_page(str(i), has_more=True, last_id=str(i)) for i in range(20))
    _mock_json_boundary(monkeypatch, lambda *args: next(pages))
    with pytest.raises(AssertionError, match="exceeded 20 pages"):
        catalog.fetch_anthropic_provider_catalog("https://workspace", "token", "c.s.mps")


def test_codex_provider_fetch_is_a_scoped_bounded_metadata_get(monkeypatch):
    payload = {"models": [{"slug": "gpt-a", "visibility": "list"}]}

    class Response(io.BytesIO):
        def getcode(self):
            return 200

    def urlopen(request, timeout):
        assert request.get_method() == "GET"
        assert request.full_url == "https://workspace/ai-gateway/codex/v1/models"
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Databricks-model-provider-service") == "c.s.mps"
        assert not request.has_header(catalog.MODEL_SERVICE_PARENT_SCHEMA_HEADER.capitalize())
        assert timeout == 30
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    result = catalog.fetch_codex_provider_catalog("https://workspace/", "token", "c.s.mps")
    assert result.model_ids == ("gpt-a",)


@pytest.mark.parametrize("ids", [["c.s.codex"], ["c.s.claude", "c.s.codex"]])
def test_codex_parent_fetch_uses_parent_scope_and_retains_exact_api_catalog(monkeypatch, ids):
    def get_json(url, headers):
        assert url == "https://workspace/ai-gateway/codex/v1/models"
        assert headers["Authorization"] == "Bearer token"
        assert headers["Databricks-Model-Service-Parent-Schema"] == "c.s"
        assert "Databricks-Model-Provider-Service" not in headers
        return {"models": [{"slug": model, "visibility": "list"} for model in ids]}

    _mock_json_boundary(monkeypatch, get_json)
    result = catalog.fetch_codex_parent_catalog("https://workspace/", "token", "c.s")
    assert result.model_ids == tuple(ids)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_provider_fetch_does_not_hide_http_failures(monkeypatch, status):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "failure", {}, None)

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    with pytest.raises(AssertionError, match=f"HTTP {status}"):
        catalog.fetch_codex_provider_catalog("https://workspace", "token", "c.s.mps")


def test_anthropic_parent_catalog_retains_pages_labels_and_scoped_requests(monkeypatch):
    pages = [
        {
            "data": [{"id": "catalog.models.claude_sonnet", "display_name": "Sonnet"}],
            "has_more": True,
            "last_id": "cursor with / and ?",
        },
        {
            "data": [{"id": "anthropic-aigw-12345678-catalog.models.kimi"}],
            "has_more": False,
        },
    ]
    requests = []

    class CatalogOpener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer test-bearer"
            assert (
                request.get_header(catalog.MODEL_SERVICE_PARENT_SCHEMA_HEADER.capitalize())
                == "catalog.models"
            )
            assert request.get_header("Anthropic-version") == "2023-06-01"
            assert not request.has_header("Databricks-model-provider-service")
            return io.BytesIO(json.dumps(pages[len(requests) - 1]).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", CatalogOpener().open)
    result = catalog.fetch_anthropic_parent_catalog(
        "https://workspace.invalid/", "test-bearer", "catalog.models"
    )
    assert result.payloads == tuple(pages)
    assert result.model_ids == (pages[0]["data"][0]["id"], pages[1]["data"][0]["id"])
    assert result.display_names == {result.model_ids[0]: "Sonnet", result.model_ids[1]: None}
    assert parse_qs(urlparse(requests[1].full_url).query) == {
        "limit": ["1000"],
        "after_id": ["cursor with / and ?"],
    }


@pytest.mark.parametrize("failure_kind", ["duplicate", "cursor", "limit"])
def test_anthropic_parent_catalog_rejects_duplicate_or_unbounded_pagination(
    monkeypatch, failure_kind
):
    requests = []

    class CatalogOpener:
        def open(self, request, *, timeout):
            requests.append(request)
            position = len(requests)
            payload = {
                "data": [
                    {"id": "duplicate" if failure_kind == "duplicate" else f"model-{position}"}
                ],
                "has_more": True,
                "last_id": "repeated" if failure_kind == "cursor" else f"cursor-{position}",
            }
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", CatalogOpener().open)
    with pytest.raises(AssertionError):
        catalog.fetch_anthropic_parent_catalog(
            "https://workspace.invalid", "test-bearer", "catalog.models"
        )
    assert len(requests) == (20 if failure_kind == "limit" else 2)


def test_codex_parent_catalog_retains_unfiltered_payload_and_validates_duplicates(monkeypatch):
    payload = {
        "models": [
            {"slug": "catalog.models.gpt_luna", "display_name": "Luna", "visibility": "list"},
            {"slug": "catalog.other_models.codex_decoy", "visibility": "hidden"},
        ]
    }

    class CatalogOpener:
        def open(self, request, *, timeout):
            assert timeout == 30
            assert (
                request.get_header(catalog.MODEL_SERVICE_PARENT_SCHEMA_HEADER.capitalize())
                == "catalog.models"
            )
            assert not request.has_header("Databricks-model-provider-service")
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", CatalogOpener().open)
    result = catalog.fetch_codex_parent_catalog(
        "https://workspace.invalid", "test-bearer", "catalog.models"
    )
    assert result.payloads == (payload,)
    assert result.model_ids == ("catalog.models.gpt_luna",)
    payload["models"].append(payload["models"][0])
    with pytest.raises(AssertionError, match="repeated"):
        catalog.fetch_codex_parent_catalog(
            "https://workspace.invalid", "test-bearer", "catalog.models"
        )


@pytest.mark.parametrize(
    "fetcher", [catalog.fetch_anthropic_provider_catalog, catalog.fetch_anthropic_parent_catalog]
)
def test_anthropic_catalog_preserves_raw_pages_and_optional_labels(monkeypatch, fetcher):
    payloads = [
        {
            "data": [{"id": "b", "visibility": "hidden", "extra": [1, 2]}],
            "has_more": True,
            "last_id": "cursor",
            "metadata": {"raw": True},
        },
        {"data": [{"id": "a", "display_name": "Model A"}], "has_more": False},
    ]
    pages = iter(payloads)
    _mock_json_boundary(monkeypatch, lambda *args: next(pages))
    result = fetcher("https://workspace", "token", "catalog.models")
    assert result.payloads == tuple(payloads)
    assert result.model_ids == ("b", "a")
    assert result.display_names == {"b": None, "a": "Model A"}


@pytest.mark.parametrize(
    "fetcher", [catalog.fetch_codex_provider_catalog, catalog.fetch_codex_parent_catalog]
)
def test_codex_catalog_preserves_hidden_entries_and_order(monkeypatch, fetcher):
    payload = {
        "models": [
            {"slug": "b", "visibility": "list"},
            {"slug": "b", "visibility": "hidden", "display_name": 7},
            {"visibility": "hidden", "extra": {"raw": True}},
            {"slug": "a", "visibility": "list", "display_name": "Model A"},
        ],
        "metadata": {"raw": True},
    }
    _mock_json_boundary(monkeypatch, lambda *args: payload)
    result = fetcher("https://workspace", "token", "catalog.models")
    assert result.payloads == (payload,)
    assert result.model_ids == ("b", "a")


@pytest.mark.parametrize(
    "fetcher",
    [
        catalog.fetch_anthropic_provider_catalog,
        catalog.fetch_anthropic_parent_catalog,
        catalog.fetch_codex_provider_catalog,
        catalog.fetch_codex_parent_catalog,
    ],
)
@pytest.mark.parametrize("scope", [None, "", " ", 7])
def test_provider_catalog_requests_require_an_explicit_scope(monkeypatch, fetcher, scope):
    def unexpected_open(*args, **kwargs):
        pytest.fail("Invalid catalog scope must not reach the HTTP boundary")

    monkeypatch.setattr(catalog.urllib.request, "urlopen", unexpected_open)
    with pytest.raises(AssertionError, match="explicit catalog scope"):
        fetcher("https://workspace", "token", scope)
