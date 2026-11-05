"""Tests for workspace CIMD authorization-server metadata."""

from __future__ import annotations

import io
import json
import urllib.error

from ucode import mcp_oauth

WS = "https://ws.staging.cloud.databricks.com"


def test_workspace_cimd_metadata_controls_agent_selected_client(monkeypatch):
    metadata = {
        "client_id_metadata_document_supported": True,
        "token_endpoint_auth_methods_supported": ["none"],
    }
    monkeypatch.setattr(
        mcp_oauth.urllib.request,
        "urlopen",
        lambda req, timeout=0: io.BytesIO(json.dumps(metadata).encode()),
    )
    assert mcp_oauth.workspace_supports_cimd(WS)
    metadata["client_id_metadata_document_supported"] = False
    assert not mcp_oauth.workspace_supports_cimd(WS)
    metadata["client_id_metadata_document_supported"] = True
    metadata["token_endpoint_auth_methods_supported"] = ["client_secret_basic"]
    assert not mcp_oauth.workspace_supports_cimd(WS)


def test_pinned_client_probe_distinguishes_published_from_missing(monkeypatch):
    client_id = "https://client.example/metadata.json"
    statuses = iter((400, 401, 503))

    def fake_urlopen(request, timeout):
        body = request.data.decode()
        assert "ug-probe-invalid-code" in body
        assert "client_id=https%3A%2F%2Fclient.example%2Fmetadata.json" in body
        raise urllib.error.HTTPError(request.full_url, next(statuses), "", {}, None)

    monkeypatch.setattr(mcp_oauth.urllib.request, "urlopen", fake_urlopen)
    mcp_oauth.oauth_client_available.cache_clear()
    try:
        assert mcp_oauth.oauth_client_available(WS, client_id) is True
        assert mcp_oauth.oauth_client_available(WS, client_id) is True  # process cache
        assert mcp_oauth.oauth_client_available(WS + "/missing", client_id) is False
        assert mcp_oauth.oauth_client_available(WS + "/error", client_id) is False
    finally:
        mcp_oauth.oauth_client_available.cache_clear()
