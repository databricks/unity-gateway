"""Offline checks for the independent managed-provider metadata oracle."""

import io
import json
import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

from tests.integration.utils import provider_catalog as catalog


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


@pytest.mark.parametrize(
    "explicit_cursor", [True, False], ids=["explicit-cursor", "gateway-cursor"]
)
def test_anthropic_provider_fetch_paginates_and_rejects_cross_page_duplicates(
    monkeypatch, explicit_cursor
):
    requests = []
    last_id = "a" if explicit_cursor else None
    pages = iter([_page("a", has_more=True, last_id=last_id), _page("b")])

    def get_json(url, headers):
        requests.append((url, headers))
        return next(pages)

    monkeypatch.setattr(catalog, "_get_json", get_json)
    result = catalog.fetch_anthropic_provider_catalog("https://workspace/", "token", "c.s.mps")
    assert result.model_ids == ("a", "b")
    assert result.display_names == {"a": "a", "b": "b"}
    assert urlparse(requests[0][0]).path == "/ai-gateway/anthropic/v1/models"
    assert "after_id" not in parse_qs(urlparse(requests[0][0]).query)
    assert parse_qs(urlparse(requests[1][0]).query)["after_id"] == ["a"]
    for _, headers in requests:
        assert headers["Authorization"] == "Bearer token"
        assert headers["Databricks-Model-Provider-Service"] == "c.s.mps"

    pages = iter([_page("a", has_more=True, last_id=last_id), _page("a")])
    with pytest.raises(AssertionError, match="repeated model id"):
        catalog.fetch_anthropic_provider_catalog("https://workspace", "token", "c.s.mps")


def test_anthropic_provider_fetch_rejects_repeated_cursor(monkeypatch):
    pages = iter(
        [_page("a", has_more=True, last_id="cursor"), _page("b", has_more=True, last_id="cursor")]
    )
    monkeypatch.setattr(catalog, "_get_json", lambda *args: next(pages))
    with pytest.raises(AssertionError, match="repeated pagination cursor"):
        catalog.fetch_anthropic_provider_catalog("https://workspace", "token", "c.s.mps")


def test_anthropic_provider_fetch_has_a_page_bound(monkeypatch):
    pages = iter(_page(str(i), has_more=True, last_id=str(i)) for i in range(20))
    monkeypatch.setattr(catalog, "_get_json", lambda *args: next(pages))
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

    monkeypatch.setattr(catalog, "_get_json", get_json)
    result = catalog.fetch_codex_parent_catalog("https://workspace/", "token", "c.s")
    assert result.model_ids == tuple(ids)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_provider_fetch_does_not_hide_http_failures(monkeypatch, status):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "failure", {}, None)

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    with pytest.raises(AssertionError, match=f"HTTP {status}"):
        catalog.fetch_codex_provider_catalog("https://workspace", "token", "c.s.mps")
