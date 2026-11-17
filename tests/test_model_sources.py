"""Tests for agents/model_sources.py."""

from __future__ import annotations

import pytest

import ucode.databricks as db_mod
from ucode.agents import model_sources

WS = "https://example.databricks.com"


class TestProviderWildcardCatalogSelection:
    @pytest.mark.parametrize(
        ("provider_type", "tool", "codex_catalog", "expected"),
        [
            # Claude expands from the Anthropic catalog even on a provider type that the
            # provider-based selection used to route to an empty Codex catalog.
            ("microsoft_foundry", "claude", [], ["claude-a", "claude-b", "claude-c"]),
            # Codex expands from the Codex catalog even on an Anthropic-API provider type.
            ("amazon_bedrock", "codex", [{"slug": "gpt-a"}], ["gpt-a"]),
            # Other tools fall back to provider-based selection: Anthropic-API provider
            # types use the Anthropic catalog, everything else the Codex catalog.
            ("anthropic", "copilot", [], ["claude-a", "claude-b", "claude-c"]),
            ("microsoft_foundry", "copilot", [{"slug": "gpt-a"}], ["gpt-a"]),
        ],
    )
    def test_provider_wildcard_uses_tool_catalog(
        self, monkeypatch, provider_type, tool, codex_catalog, expected
    ):
        service = "main.test.provider"
        source = f"{service}.*"

        def get_json(url, token, **kwargs):
            if "/unity-catalog/model-provider-services/" in url:
                return {
                    "name": f"model-provider-services/{service}",
                    "config": {
                        "provider_type": provider_type,
                        "targets": [
                            {"model": "claude-a", "native_api_types": ["anthropic/v1/messages"]}
                        ],
                    },
                }, None
            assert kwargs["headers"][db_mod.MODEL_PROVIDER_SERVICE_HEADER] == service
            if "/anthropic/" in url:
                return {
                    "data": [
                        {"id": name} for name in ("claude-a", "claude-b", f"{service}.claude-c")
                    ]
                }, None
            assert "/codex/" in url
            return {"models": codex_catalog}, None

        monkeypatch.setattr(db_mod, "_http_get_json", get_json)

        models, live = model_sources.resolve_model_sources([source], tool, WS, "token")

        qualified = [f"{service}.{name}" for name in expected]
        assert models == qualified
        assert live == ({source: qualified} if qualified else {})
