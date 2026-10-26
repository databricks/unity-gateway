"""Tests for workspace CIMD authorization-server metadata."""

from __future__ import annotations

import io
import json

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
