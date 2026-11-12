"""Managed wildcard model sources expand through schema and provider catalogs."""

import pytest

import ucode.databricks as db_mod
from ucode.agents import claude, codex, copilot, model_sources
from ucode.state import get_managed_model_source_expansions, set_managed_model_source_expansions

WS = "https://example.cloud.databricks.com"


@pytest.mark.parametrize("tool", ["claude", "copilot", "codex"])
def test_expand_schema_wildcard_uses_native_ids_and_deduplicates(monkeypatch, tool):
    calls = []

    def catalog(workspace, token, *, parent_schema=None, provider=None):
        calls.append((workspace, token, parent_schema, provider))
        return db_mod.AnthropicModelCatalog(
            model_ids=[
                "system.ai.claude-opus-5-5",
                "anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol",
            ],
            model_id_to_display_name={},
        )

    def codex_catalog(workspace, token, *, source, identifier):
        calls.append((workspace, token, source, identifier))
        return {
            "models": [
                {"slug": "system.ai.claude-opus-5-5"},
                {"slug": "anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol"},
            ]
        }

    monkeypatch.setattr(claude, "list_anthropic_model_catalog", catalog)
    monkeypatch.setattr(copilot, "list_anthropic_model_catalog", catalog)
    monkeypatch.setattr(codex, "_fetch_codex_model_catalog", codex_catalog)
    expanded, live = model_sources.expand_wildcard_sources(
        ["system.ai.claude-opus-5-5", "system.ai.*", "system.ai.*"], tool, WS, "token"
    )
    second = (
        "system.ai.gpt-6-1-sol"
        if tool == "copilot"
        else "anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol"
    )
    assert expanded == ["system.ai.claude-opus-5-5", second]
    assert live == {"system.ai.*": ["system.ai.claude-opus-5-5", second]}
    assert calls == [
        (WS, "token", db_mod.CodexCatalogSource.PARENT_SCHEMA, "system.ai")
        if tool == "codex"
        else (WS, "token", "system.ai", None)
    ]


@pytest.mark.parametrize("tool", ["claude", "codex", "copilot"])
def test_expand_wildcard_sources_in_place_and_deduplicates(monkeypatch, tool):
    monkeypatch.setattr(
        db_mod, "get_model_provider_service", lambda *_: ({"provider_type": "custom"}, None)
    )
    calls = []

    def catalog(workspace, token, *, parent_schema=None):
        calls.append((workspace, token, parent_schema))
        return ["system.ai.kimi", "system.ai.claude-sonnet"], None

    def provider(workspace, token, source_name, provider_type):
        calls.append((workspace, token, source_name, provider_type))
        return ["one", "c.s.mps.two"], None

    monkeypatch.setattr(db_mod, "list_provider_service_models", provider)
    monkeypatch.setattr(
        {"claude": claude, "codex": codex, "copilot": copilot}[tool], "source_models", catalog
    )
    sources = ["system.ai.kimi", "c.s.mps.*", "c.s.mps.one", "system.ai.*"]
    expanded, live = model_sources.expand_wildcard_sources(sources, tool, WS, "token")
    assert expanded == [
        "system.ai.kimi",
        "c.s.mps.one",
        "c.s.mps.two",
        "system.ai.claude-sonnet",
    ]
    assert live == {
        "c.s.mps.*": ["c.s.mps.one", "c.s.mps.two"],
        "system.ai.*": ["system.ai.kimi", "system.ai.claude-sonnet"],
    }
    assert calls == [
        (WS, "token", "c.s.mps", "custom"),
        (WS, "token", "system.ai"),
    ]


@pytest.mark.parametrize("service", [(None, "403"), ({"provider_type": "custom"}, None)])
def test_expand_wildcard_sources_drops_failed_lookup(monkeypatch, service):
    monkeypatch.setattr(db_mod, "get_model_provider_service", lambda *_: service)
    monkeypatch.setattr(db_mod, "list_provider_service_models", lambda *a: ([], None))
    warnings = []
    monkeypatch.setattr(model_sources, "print_warning", warnings.append)
    sources = ["c.s.mps.*", "system.ai.model", "c.s.mps.*"]
    assert model_sources.expand_wildcard_sources(sources, "claude", WS, "token") == (
        ["system.ai.model"],
        {},
    )
    assert len(warnings) == 1
    assert "c.s.mps.*" in warnings[0]


def test_expand_wildcard_sources_preserves_other_forms(monkeypatch):
    def fail(*args):
        pytest.fail("unexpected lookup")

    monkeypatch.setattr(db_mod, "get_model_provider_service", fail)
    sources = ["c.s.mps.model", "c.s.*.model", "c.*.mps.*", "system.*.*", "*.ai.*"]
    assert model_sources.expand_wildcard_sources(sources, "claude", WS, "token") == (sources, {})


@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_provider_wildcard_uses_service_type_catalog(monkeypatch, provider_type):
    monkeypatch.setattr(
        db_mod, "get_model_provider_service", lambda *_: ({"provider_type": provider_type}, None)
    )
    urls = []
    targets = [
        "global.openai.gpt-6.1-sol",
        "us-gov-west-1/anthropic.claude-haiku-5-5",
        "claude-opus-4-7@default",
        "deepseek-ai/deepseek-v4.1-flash-maas",
        "eu/gpt-6.1-sol",
        "FW-Kimi-K2.5",
        "gpt-5.5-2026-04-23",
    ]

    def get_catalog(url, token, **kwargs):
        urls.append(url)
        return {
            "data": [{"id": target} for target in targets],
            "models": [{"slug": target} for target in targets],
        }, None

    monkeypatch.setattr(db_mod, "_http_get_json", get_catalog)
    anthropic = provider_type == "anthropic"
    qualified = [f"c.s.mps.{target}" for target in targets]
    assert model_sources.expand_wildcard_sources(["c.s.mps.*"], "claude", WS, "token") == (
        qualified,
        {"c.s.mps.*": qualified},
    )
    assert len(urls) == 1
    assert ("/anthropic/" if anthropic else "/codex/") in urls[0]


def test_fallback_applies_per_wildcard_only_and_records_live_results(monkeypatch):
    calls = []
    warnings = []

    def lookup(model, *args):
        calls.append(model)
        if model == "system.ai.*":
            return ["system.ai.live"], None
        return [], "403"

    monkeypatch.setattr(model_sources, "_expand_wildcard_source", lookup)
    monkeypatch.setattr(model_sources, "print_warning", warnings.append)
    expanded, live = model_sources.expand_wildcard_sources(
        ["c.s.mps.*", "system.ai.*", "c.s.mps.*", "other.ai.*"],
        "claude",
        WS,
        "token",
        fallbacks={"c.s.mps.*": ["c.s.mps.saved"]},
    )
    assert expanded == ["c.s.mps.saved", "system.ai.live"]
    assert live == {"system.ai.*": ["system.ai.live"]}
    assert calls == ["c.s.mps.*", "system.ai.*", "other.ai.*"]
    assert len(warnings) == 2
    assert "last configure" in warnings[0] and "403" in warnings[0]
    assert "retry" in warnings[1]


def test_live_expansion_wins_over_fallback(monkeypatch):
    warnings = []
    monkeypatch.setattr(model_sources, "print_warning", warnings.append)
    monkeypatch.setattr(
        model_sources, "_expand_wildcard_source", lambda *a: (["system.ai.live"], None)
    )
    fallbacks = {"system.ai.*": ["system.ai.saved"]}
    assert model_sources.expand_wildcard_sources(
        ["system.ai.*"], "claude", WS, "token", fallbacks=fallbacks
    ) == (["system.ai.live"], {"system.ai.*": ["system.ai.live"]})
    assert not warnings


@pytest.mark.parametrize("invalid", [[], "model", [""], ["ok", None]])
def test_malformed_fallback_is_not_used(invalid):
    state = {
        "managed_model_source_expansions": {
            "claude": {"system.ai.*": invalid, "valid.ai.*": ["valid.ai.model"]}
        }
    }
    assert get_managed_model_source_expansions(state, "claude") == {
        "valid.ai.*": ["valid.ai.model"]
    }


@pytest.mark.parametrize("saved", [None, [], "invalid", {"claude": []}])
def test_malformed_saved_expansion_container_is_ignored(saved):
    assert (
        get_managed_model_source_expansions({"managed_model_source_expansions": saved}, "claude")
        == {}
    )


def test_set_expansions_preserves_other_agents_and_removes_empty_mapping():
    state = {"managed_model_source_expansions": {"codex": {"system.ai.*": ["system.ai.codex"]}}}
    set_managed_model_source_expansions(state, "claude", {"system.ai.*": ["system.ai.claude"]})
    set_managed_model_source_expansions(state, "claude", {})
    assert get_managed_model_source_expansions(state, "codex") == {
        "system.ai.*": ["system.ai.codex"]
    }
    set_managed_model_source_expansions(state, "codex", {})
    assert "managed_model_source_expansions" not in state
