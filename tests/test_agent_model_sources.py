"""Managed wildcard model sources expand through schema and provider catalogs."""

import pytest

import ucode.agents as agents
import ucode.databricks as db_mod
from ucode.agents import claude, codex, copilot
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
    expanded, live = agents.expand_wildcard_sources(
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
    monkeypatch.setattr(agents._MODULES[tool], "source_models", catalog)
    models = ["system.ai.kimi", "c.s.mps.*", "c.s.mps.one", "system.ai.*"]
    expanded, live = agents.expand_wildcard_sources(models, tool, WS, "token")
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
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    models = ["c.s.mps.*", "system.ai.model", "c.s.mps.*"]
    assert agents.expand_wildcard_sources(models, "claude", WS, "token") == (
        ["system.ai.model"],
        {},
    )
    assert len(warnings) == 1
    assert "c.s.mps.*" in warnings[0]


def test_expand_wildcard_sources_preserves_other_forms(monkeypatch):
    def fail(*args):
        pytest.fail("unexpected lookup")

    monkeypatch.setattr(db_mod, "get_model_provider_service", fail)
    models = ["c.s.mps.model", "c.s.*.model", "c.*.mps.*", "system.*.*", "*.ai.*"]
    assert agents.expand_wildcard_sources(models, "claude", WS, "token") == (models, {})


@pytest.mark.parametrize("source", ["c.s.mps.*", "system.ai.*"])
def test_expand_wildcard_sources_without_token_names_the_missing_token(monkeypatch, source):
    def fail(*args):
        pytest.fail("unexpected lookup without a token")

    monkeypatch.setattr(db_mod, "get_model_provider_service", fail)
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    assert agents.expand_wildcard_sources([source], "claude", WS, "") == ([], {})
    assert len(warnings) == 1
    assert "no Databricks token; run `ug configure`" in warnings[0]


@pytest.mark.parametrize("tool", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_provider_wildcard_uses_service_type_catalog(monkeypatch, tool, provider_type):
    monkeypatch.setattr(
        db_mod, "get_model_provider_service", lambda *_: ({"provider_type": provider_type}, None)
    )
    urls = []

    def get_catalog(url, token, **kwargs):
        urls.append(url)
        return {"data": [{"id": "anthropic-model"}], "models": [{"slug": "codex-model"}]}, None

    monkeypatch.setattr(db_mod, "_http_get_json", get_catalog)
    anthropic = provider_type == "anthropic"
    expected = "anthropic-model" if anthropic else "codex-model"
    qualified = f"c.s.mps.{expected}"
    assert agents.expand_wildcard_sources(["c.s.mps.*"], tool, WS, "token") == (
        [qualified],
        {"c.s.mps.*": [qualified]},
    )
    assert len(urls) == 1
    assert ("/anthropic/" if anthropic else "/codex/") in urls[0]


@pytest.mark.parametrize("provider_type", ["anthropic", "openai"])
def test_provider_empty_listing_is_not_an_error(monkeypatch, provider_type):
    monkeypatch.setattr(
        db_mod, "get_model_provider_service", lambda *_: ({"provider_type": provider_type}, None)
    )
    monkeypatch.setattr(
        db_mod, "_http_get_json", lambda *a, **k: ({"data": [], "models": []}, None)
    )
    assert db_mod.list_model_provider_service_targets(WS, "token", "c.s.mps") == ([], None)


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
    expanded, live = agents.expand_wildcard_sources(
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


def test_live_expansion_wins_over_fallback_and_missing_token_can_use_it(monkeypatch):
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)
    monkeypatch.setattr(agents, "_expand_wildcard_source", lambda *a: (["system.ai.live"], None))
    fallbacks = {"system.ai.*": ["system.ai.saved"]}
    assert agents.expand_wildcard_sources(
        ["system.ai.*"], "claude", WS, "token", fallbacks=fallbacks
    ) == (["system.ai.live"], {"system.ai.*": ["system.ai.live"]})
    assert not warnings
    assert agents.expand_wildcard_sources(
        ["system.ai.*"], "claude", WS, "", fallbacks=fallbacks
    ) == (["system.ai.saved"], {})
    assert "no Databricks token" in warnings[0] and "last configure" in warnings[0]


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


@pytest.mark.parametrize("tool", ["claude", "codex"])
@pytest.mark.parametrize("source", ["c.s.mps.*", "system.ai.*"])
def test_empty_catalog_warning_keeps_schema_reason_only(monkeypatch, tool, source):
    monkeypatch.setattr(
        db_mod, "get_model_provider_service", lambda *_: ({"provider_type": "custom"}, None)
    )
    monkeypatch.setattr(
        db_mod, "_http_get_json", lambda *a, **k: ({"data": [], "models": []}, None)
    )
    warnings = []
    monkeypatch.setattr(agents, "print_warning", warnings.append)

    assert agents.expand_wildcard_sources([source], tool, WS, "token") == ([], {})
    assert len(warnings) == 1
    if source == "c.s.mps.*":
        assert f"{source};" in warnings[0]
    else:
        assert f"{source} (" in warnings[0]
