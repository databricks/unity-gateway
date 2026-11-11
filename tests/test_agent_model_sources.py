"""Managed wildcard model sources expand through each agent's native catalog."""

import pytest

import ucode.agents as agents
import ucode.databricks as db_mod
from ucode.agents import claude, codex, copilot

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
    assert agents.expand_wildcard_sources(
        ["system.ai.claude-opus-5-5", "system.ai.*", "system.ai.*"], tool, WS, "token"
    ) == [
        "system.ai.claude-opus-5-5",
        "system.ai.gpt-6-1-sol"
        if tool == "copilot"
        else "anthropic-aigw-c4442a48-system.ai.gpt-6-1-sol",
    ]
    assert calls == [
        (WS, "token", db_mod.CodexCatalogSource.PARENT_SCHEMA, "system.ai")
        if tool == "codex"
        else (WS, "token", "system.ai", None)
    ]


@pytest.mark.parametrize("tool", ["claude", "codex", "copilot"])
def test_expand_wildcard_sources_in_place_and_deduplicates(monkeypatch, tool):
    monkeypatch.setattr(
        agents, "get_model_provider_service", lambda *_: ({"provider_type": "custom"}, None)
    )
    calls = []

    def catalog(workspace, token, *, parent_schema=None, provider=None, provider_service=None):
        calls.append((workspace, token, parent_schema, provider, provider_service))
        if provider:
            return ["one", "c.s.mps.two"], None
        return ["system.ai.kimi", "system.ai.claude-sonnet"], None

    monkeypatch.setattr(agents._MODULES[tool], "source_models", catalog)
    models = ["system.ai.kimi", "c.s.mps.*", "c.s.mps.one", "system.ai.*"]
    assert agents.expand_wildcard_sources(models, tool, WS, "token") == [
        "system.ai.kimi",
        "c.s.mps.one",
        "c.s.mps.two",
        "system.ai.claude-sonnet",
    ]
    assert calls == [
        (WS, "token", None, "c.s.mps", {"provider_type": "custom"}),
        (WS, "token", "system.ai", None, None),
    ]


@pytest.mark.parametrize("service", [(None, "403"), ({"provider_type": "custom"}, None)])
def test_expand_wildcard_sources_drops_failed_lookup(monkeypatch, service):
    monkeypatch.setattr(agents, "get_model_provider_service", lambda *_: service)
    monkeypatch.setattr(claude, "source_models", lambda *a, **k: ([], None))
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    models = ["c.s.mps.*", "system.ai.model", "c.s.mps.*"]
    assert agents.expand_wildcard_sources(models, "claude", WS, "token") == ["system.ai.model"]
    assert len(warnings) == 1
    assert "c.s.mps.*" in warnings[0]


def test_expand_wildcard_sources_preserves_other_forms(monkeypatch):
    def fail(*args):
        pytest.fail("unexpected lookup")

    monkeypatch.setattr(agents, "get_model_provider_service", fail)
    models = ["c.s.mps.model", "c.s.*.model", "c.*.mps.*", "system.*.*", "*.ai.*"]
    assert agents.expand_wildcard_sources(models, "claude", WS, "token") == models


@pytest.mark.parametrize("source", ["c.s.mps.*", "system.ai.*"])
def test_expand_wildcard_sources_without_token_names_the_missing_token(monkeypatch, source):
    def fail(*args):
        pytest.fail("unexpected lookup without a token")

    monkeypatch.setattr(agents, "get_model_provider_service", fail)
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    assert agents.expand_wildcard_sources([source], "claude", WS, "") == []
    assert len(warnings) == 1
    assert "no Databricks token; run `ug configure`" in warnings[0]


@pytest.mark.parametrize("tool", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_provider_wildcard_uses_the_agent_catalog(monkeypatch, tool, provider_type):
    monkeypatch.setattr(
        agents, "get_model_provider_service", lambda *_: ({"provider_type": provider_type}, None)
    )
    urls = []

    def get_catalog(url, token, **kwargs):
        urls.append(url)
        return {"data": [{"id": "anthropic-model"}], "models": [{"slug": "codex-model"}]}, None

    monkeypatch.setattr(db_mod, "_http_get_json", get_catalog)
    anthropic = provider_type == "anthropic"
    expected = "anthropic-model" if anthropic else "codex-model"
    assert agents.expand_wildcard_sources(["c.s.mps.*"], tool, WS, "token") == [
        f"c.s.mps.{expected}"
    ]
    assert len(urls) == 1
    assert ("/anthropic/" if anthropic else "/codex/") in urls[0]


@pytest.mark.parametrize("tool", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_provider_empty_listing_is_not_an_error(monkeypatch, tool, provider_type):
    monkeypatch.setattr(
        db_mod, "_http_get_json", lambda *a, **k: ({"data": [], "models": []}, None)
    )
    assert agents._MODULES[tool].source_models(
        WS, "token", provider="c.s.mps", provider_service={"provider_type": provider_type}
    ) == ([], None)


def test_codex_empty_schema_still_raises(monkeypatch):
    monkeypatch.setattr(db_mod, "_http_get_json", lambda *a, **k: ({"models": []}, None))
    with pytest.raises(db_mod.CodexModelCatalogEmpty):
        codex.source_models(WS, "token", parent_schema="system.ai")


def test_fallback_applies_per_wildcard_only_and_records_live_results(monkeypatch):
    calls = []
    warnings = []

    def lookup(model, *args):
        calls.append(model)
        if model == "system.ai.*":
            return ["system.ai.live"], None
        return [], "403"

    monkeypatch.setattr(agents, "_expand_wildcard_source", lookup)
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    live = {}
    assert agents.expand_wildcard_sources(
        ["c.s.mps.*", "system.ai.*", "c.s.mps.*", "other.ai.*"],
        "claude",
        WS,
        "token",
        fallbacks={"c.s.mps.*": ["c.s.mps.saved"]},
        live_expansions=live,
    ) == ["c.s.mps.saved", "system.ai.live"]
    assert live == {"system.ai.*": ["system.ai.live"]}
    assert calls == ["c.s.mps.*", "system.ai.*", "other.ai.*"]
    assert len(warnings) == 2
    assert "last configure" in warnings[0] and "403" in warnings[0]
    assert "retry" in warnings[1]


def test_live_expansion_wins_over_fallback_and_missing_token_can_use_it(monkeypatch):
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    monkeypatch.setattr(agents, "_expand_wildcard_source", lambda *a: (["system.ai.live"], None))
    fallbacks = {"system.ai.*": ["system.ai.saved"]}
    assert agents.expand_wildcard_sources(
        ["system.ai.*"], "claude", WS, "token", fallbacks=fallbacks
    ) == ["system.ai.live"]
    assert not warnings
    assert agents.expand_wildcard_sources(
        ["system.ai.*"], "claude", WS, "", fallbacks=fallbacks
    ) == ["system.ai.saved"]
    assert "no Databricks token" in warnings[0] and "last configure" in warnings[0]


@pytest.mark.parametrize("invalid", [[], "model", [""], ["ok", None]])
def test_malformed_fallback_is_not_used(monkeypatch, invalid):
    monkeypatch.setattr(agents, "_expand_wildcard_source", lambda *a: ([], "403"))
    assert (
        agents.expand_wildcard_sources(
            ["system.ai.*"], "claude", WS, "token", fallbacks={"system.ai.*": invalid}
        )
        == []
    )


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("source", ["c.s.mps.*", "system.ai.*"])
def test_empty_catalog_warning_keeps_schema_reason_only(monkeypatch, tool, source):
    monkeypatch.setattr(
        agents, "get_model_provider_service", lambda *_: ({"provider_type": "custom"}, None)
    )
    monkeypatch.setattr(
        db_mod, "_http_get_json", lambda *a, **k: ({"data": [], "models": []}, None)
    )
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)

    assert agents.expand_wildcard_sources([source], tool, WS, "token") == []
    assert len(warnings) == 1
    if source == "c.s.mps.*":
        assert f"{source};" in warnings[0]
    else:
        assert f"{source} (" in warnings[0]
