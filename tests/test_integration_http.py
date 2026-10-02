"""Offline component checks for read-only workspace HTTP boundaries."""

import io
import urllib.error
import urllib.response

import pytest

from tests.integration.utils import http, provider_catalog

FETCHERS = [
    provider_catalog.fetch_anthropic_parent_catalog,
    provider_catalog.fetch_codex_parent_catalog,
    provider_catalog.fetch_anthropic_provider_catalog,
    provider_catalog.fetch_codex_provider_catalog,
]


def fetch_inventory(fetcher, workspace="https://workspace.invalid", token="test-bearer"):
    return fetcher(workspace, token, "catalog.models")


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize(
    "workspace",
    [
        None,
        7,
        "",
        "http://workspace.invalid",
        "https://user:secret@workspace.invalid",
        "https://workspace.invalid/path",
        "https://workspace.invalid?secret=value",
        "https://workspace.invalid#fragment",
        "https:///missing-host",
        "https://workspace.invalid:bad",
        "https://workspace.invalid:0",
        "https://workspace.invalid:70000",
        "https://workspace.invalid\n",
        "https://workspace.invalid\\@other.invalid",
    ],
)
def test_catalog_gets_reject_invalid_workspace_origins(monkeypatch, fetcher, workspace):
    def unexpected_open(*args, **kwargs):
        pytest.fail("Invalid workspace must not reach the HTTP boundary")

    monkeypatch.setattr(http.urllib.request, "build_opener", unexpected_open)
    with pytest.raises(AssertionError, match="HTTPS workspace origin") as failure:
        fetch_inventory(fetcher, workspace)
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("token", [None, "", " ", "bearer\nsecret", "bearer\rsecret"])
def test_catalog_gets_require_explicit_valid_bearers(monkeypatch, fetcher, token):
    def unexpected_open(*args, **kwargs):
        pytest.fail("Invalid bearer must not reach the HTTP boundary")

    monkeypatch.setattr(http.urllib.request, "build_opener", unexpected_open)
    with pytest.raises(AssertionError, match="explicit workspace bearer"):
        fetch_inventory(fetcher, token=token)


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("failure_kind", ["http", "url", "os", "json", "encoding"])
def test_catalog_get_errors_are_sanitized(monkeypatch, fetcher, failure_kind):
    secret = "private-bearer-and-response-secret"

    class FailedOpener:
        def open(self, request, *, timeout):
            assert timeout == 30
            assert request.get_method() == "GET"
            assert request.get_header("Authorization") == f"Bearer {secret}"
            if failure_kind == "http":
                raise urllib.error.HTTPError(request.full_url, 403, secret, {}, None)
            if failure_kind == "url":
                raise urllib.error.URLError(secret)
            if failure_kind == "os":
                raise OSError(secret)
            return io.BytesIO(secret.encode() if failure_kind == "json" else b"\xff")

    monkeypatch.setattr(http.urllib.request, "build_opener", lambda *_: FailedOpener())
    with pytest.raises(AssertionError, match="Workspace JSON GET") as failure:
        fetch_inventory(fetcher, token=secret)
    assert secret not in str(failure.value)
    assert "workspace.invalid" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["https://other.invalid/secret", "/redirected"])
def test_catalog_gets_deny_redirects(monkeypatch, fetcher, status, destination):
    requests = []
    build_opener = http.urllib.request.build_opener

    class BoundaryHTTPS(http.urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            response = urllib.response.addinfourl(
                io.BytesIO(b"{}"), {"Location": destination}, request.full_url, status
            )
            response.msg = "private-redirect-reason"
            return response

    monkeypatch.setattr(
        http.urllib.request,
        "build_opener",
        lambda handler: build_opener(handler, BoundaryHTTPS()),
    )
    with pytest.raises(AssertionError, match=f"HTTP {status}") as failure:
        fetch_inventory(fetcher)
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer test-bearer"
    assert destination not in str(failure.value)
    assert "private-redirect-reason" not in str(failure.value)
