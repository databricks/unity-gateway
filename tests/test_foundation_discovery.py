"""Component coverage for sharing one listing across requested API families."""

from unittest.mock import Mock

import pytest

from ucode import databricks as db
from ucode.constants import API_GEMINI_GENERATE_CONTENT, API_OPENAI_RESPONSES

WORKSPACE = "https://example.databricks.com"
FAMILIES = [API_GEMINI_GENERATE_CONTENT, API_OPENAI_RESPONSES]


def endpoint(name, api_types, *, v2=True):
    return {
        "name": name,
        "config": {
            "served_entities": [
                {"foundation_model": {"ai_gateway_v2_supported": v2, "api_types": api_types}}
            ]
        },
    }


def test_families_share_one_request_and_preserve_filtering_and_order(monkeypatch):
    """Scenario: Pi needs both families. Expected: one GET, sorted v2-compatible models."""
    get = Mock(
        return_value=(
            {
                "endpoints": [
                    endpoint("gemini-2-5-flash", [API_GEMINI_GENERATE_CONTENT]),
                    endpoint("gpt-5-4", [API_OPENAI_RESPONSES]),
                    endpoint("gemini-3-5-flash", [API_GEMINI_GENERATE_CONTENT]),
                    endpoint("gpt-5-5", [API_OPENAI_RESPONSES]),
                    endpoint("v1-only", FAMILIES, v2=False),
                    endpoint("embedding", ["openai/v1/embeddings"]),
                ]
            },
            None,
        )
    )
    monkeypatch.setattr(db, "_http_get_json", get)

    result = db.discover_endpoints_with_api_types(
        WORKSPACE, "token", FAMILIES, sort_key=db.model_version_sort_key
    )

    assert result == {
        API_GEMINI_GENERATE_CONTENT: (["gemini-3-5-flash", "gemini-2-5-flash"], None),
        API_OPENAI_RESPONSES: (["gpt-5-5", "gpt-5-4"], None),
    }
    get.assert_called_once_with(f"{WORKSPACE}/api/2.0/serving-endpoints:foundation-models", "token")


@pytest.mark.parametrize(
    "payload, reason, expected",
    [
        (None, "HTTP 403 Forbidden", "HTTP 403 Forbidden"),
        ({}, None, "foundation-models listing returned no endpoints"),
        ({"endpoints": [endpoint("old", FAMILIES, v2=False)]}, None, "v1-only endpoints"),
        ({"endpoints": [endpoint("embedding", ["embeddings"])]}, None, "no endpoint exposes"),
    ],
)
def test_each_family_retains_diagnostics(monkeypatch, payload, reason, expected):
    """Scenario: listing fails or lacks usable models. Expected: each family keeps its reason."""
    get = Mock(return_value=(payload, reason))
    monkeypatch.setattr(db, "_http_get_json", get)

    results = db.discover_endpoints_with_api_types(WORKSPACE, "token", FAMILIES)

    assert get.call_count == 1
    for api_type, (models, diagnostic) in results.items():
        assert models == []
        assert expected in diagnostic
        if expected in ("v1-only endpoints", "no endpoint exposes"):
            assert api_type in diagnostic


def test_empty_request_needs_no_listing(monkeypatch):
    """Scenario: no families requested. Expected: no network call."""
    get = Mock(side_effect=AssertionError("unexpected request"))
    monkeypatch.setattr(db, "_http_get_json", get)
    assert db.discover_endpoints_with_api_types(WORKSPACE, "token", []) == {}


def test_each_discovery_is_fresh_and_failure_does_not_poison_next_call(monkeypatch):
    """Scenario: credentials or server state change. Expected: every discovery reads afresh."""
    get = Mock(
        side_effect=[
            (None, "network error"),
            ({"endpoints": [endpoint("available", FAMILIES)]}, None),
            ({"endpoints": []}, None),
        ]
    )
    monkeypatch.setattr(db, "_http_get_json", get)

    first = db.discover_endpoints_with_api_types(WORKSPACE, "old-token", FAMILIES)
    second = db.discover_endpoints_with_api_types(WORKSPACE, "new-token", FAMILIES)
    third = db.discover_endpoints_with_api_types(WORKSPACE, "new-token", FAMILIES)

    assert first[API_OPENAI_RESPONSES] == ([], "network error")
    assert second[API_OPENAI_RESPONSES] == (["available"], None)
    assert third[API_OPENAI_RESPONSES][0] == []
    assert get.call_count == 3
