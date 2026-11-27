"""Tests for pricing.py — the opt-in system.ai model pricing feature."""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest

from ucode import pricing

WS = "https://example.databricks.com"

GLM_RATES = {
    "system.ai.glm-5-3": {
        "DBU": {"input": 20.0, "output": 62.85714, "cache_read": 3.71428},
        "USD": {"input": 1.4, "output": 4.3999998, "cache_read": 0.2599996},
    }
}


@pytest.fixture(autouse=True)
def _pricing_default_off(monkeypatch):
    monkeypatch.delenv(pricing.MODEL_PRICING_ENV, raising=False)


class TestModelPricingEnabled:
    def test_disabled_by_default(self):
        assert pricing.model_pricing_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_opt_in(self, monkeypatch, value):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, value)
        assert pricing.model_pricing_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "maybe", "2"])
    def test_other_values_stay_off(self, monkeypatch, value):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, value)
        assert pricing.model_pricing_enabled() is False


class TestFetchModelServiceRates:
    def test_parses_dbu_and_usd_rates_by_token_kind(self, monkeypatch):
        seen = {}

        def fake_get(url, token, **kwargs):
            seen.update(url=url, token=token)
            return (
                {
                    "model_service_rates": [
                        {
                            "model_service": "system.ai.glm-5-3",
                            "costs": [
                                {
                                    "unit": "DBU",
                                    "token_costs": [
                                        {"token_type": "TOKEN_TYPE_INPUT", "cost": 20.0},
                                        {"token_type": "TOKEN_TYPE_OUTPUT", "cost": 62.85714},
                                        {"token_type": "TOKEN_TYPE_CACHE_READ", "cost": 3.71428},
                                    ],
                                },
                                {
                                    "unit": "USD",
                                    "token_costs": [
                                        {"token_type": "TOKEN_TYPE_INPUT", "cost": 1.4},
                                        {"token_type": "TOKEN_TYPE_OUTPUT", "cost": 4.3999998},
                                        {"token_type": "TOKEN_TYPE_CACHE_READ", "cost": 0.2599996},
                                    ],
                                },
                            ],
                        }
                    ]
                },
                None,
            )

        monkeypatch.setattr(pricing, "_http_get_json", fake_get)

        rates, reason = pricing.fetch_model_service_rates(
            WS, "workspace-token", ["system.ai.glm-5-3"]
        )

        assert reason is None
        assert rates == GLM_RATES
        assert seen["token"] == "workspace-token"
        assert seen["url"].startswith(f"{WS}/api/ai-gateway/v2/endpoint-rates:batchGet?")
        assert parse_qs(seen["url"].split("?", 1)[1])["databricks_hosted_model_services"] == [
            "system.ai.glm-5-3"
        ]

    def test_repeats_the_query_param_and_skips_malformed_entries(self, monkeypatch):
        urls = []

        def fake_get(url, token, **kwargs):
            urls.append(url)
            return (
                {
                    "model_service_rates": [
                        {"model_service": "system.ai.claude-opus-4-8", "costs": "not-a-list"},
                        {"costs": []},
                        {"model_service": "", "costs": []},
                        {
                            "model_service": "system.ai.glm-5-3",
                            "costs": [
                                {
                                    "unit": "DBU",
                                    "token_costs": [
                                        {
                                            "token_type": "TOKEN_TYPE_CACHE_CREATION_1H",
                                            "cost": True,
                                        },
                                        {"token_type": "TOKEN_TYPE_OUTPUT", "cost": 1},
                                        {"token_type": "TOKEN_TYPE_UNKNOWN", "cost": 5},
                                    ],
                                }
                            ],
                        },
                    ]
                },
                None,
            )

        monkeypatch.setattr(pricing, "_http_get_json", fake_get)

        rates, reason = pricing.fetch_model_service_rates(
            WS, "workspace-token", ["system.ai.claude-opus-4-8", "system.ai.glm-5-3"]
        )

        assert reason is None
        assert rates == {
            "system.ai.glm-5-3": {"DBU": {"output": 1.0}},
        }
        assert parse_qs(urls[0].split("?", 1)[1])["databricks_hosted_model_services"] == [
            "system.ai.claude-opus-4-8",
            "system.ai.glm-5-3",
        ]

    def test_empty_request_short_circuits_without_a_call(self, monkeypatch):
        monkeypatch.setattr(
            pricing, "_http_get_json", lambda *args, **kwargs: pytest.fail("must not call")
        )

        rates, reason = pricing.fetch_model_service_rates(WS, "workspace-token", [])

        assert (rates, reason) == ({}, None)

    def test_failure_reason_passes_through(self, monkeypatch):
        monkeypatch.setattr(
            pricing, "_http_get_json", lambda *args, **kwargs: (None, "HTTP 404 Not Found")
        )

        rates, reason = pricing.fetch_model_service_rates(
            WS, "workspace-token", ["system.ai.glm-5-3"]
        )

        assert rates == {}
        assert reason == "HTTP 404 Not Found"

    def test_rejects_non_dict_payload(self, monkeypatch):
        monkeypatch.setattr(pricing, "_http_get_json", lambda *args, **kwargs: ([], None))

        rates, reason = pricing.fetch_model_service_rates(
            WS, "workspace-token", ["system.ai.glm-5-3"]
        )

        assert rates == {}
        assert reason == "endpoint-rates returned an unexpected response shape"


class TestPricingFqn:
    def test_plain_and_1m_ids_map_to_their_fqn(self):
        assert pricing.pricing_fqn("system.ai.glm-5-3") == "system.ai.glm-5-3"
        assert pricing.pricing_fqn("system.ai.claude-opus-4-8[1m]") == "system.ai.claude-opus-4-8"

    def test_discovery_aliases_unwrap_to_the_embedded_fqn(self):
        assert (
            pricing.pricing_fqn("anthropic-aigw-982d9f93-system.ai.glm-5-3") == "system.ai.glm-5-3"
        )
        assert pricing.pricing_fqn("anthropic-aigw-1-system.ai.glm-5-3[1m]") == "system.ai.glm-5-3"

    def test_non_system_ai_ids_name_nothing(self):
        assert pricing.pricing_fqn("main.default.claude-opus") is None
        assert pricing.pricing_fqn("claude-opus-4-8") is None


class TestUsdPricingOverrides:
    GLM_OVERRIDES = {
        "input": 1.4,
        "output": 4.4,
        "cacheRead": 0.26,
        # No cache-creation rate is served, so the cache write falls back to the input rate.
        "cacheWrite": 1.4,
    }

    def test_builds_rows_from_usd_rates(self):
        assert pricing.usd_pricing_overrides(GLM_RATES) == {"system.ai.glm-5-3": self.GLM_OVERRIDES}

    def test_cache_write_prefers_the_1h_rate_then_the_5m_rate(self):
        overrides = pricing.usd_pricing_overrides(
            {
                "system.ai.claude-opus-4-8": {
                    "USD": {
                        "input": 5.0,
                        "output": 25.0,
                        "cache_read": 0.5,
                        "cache_creation": 6.25,
                        "cache_creation_1h": 10.0,
                    }
                },
                "system.ai.claude-haiku-4-5": {
                    "USD": {
                        "input": 0.8,
                        "output": 4.0,
                        "cache_read": 0.08,
                        "cache_creation": 1.0,
                    }
                },
            }
        )

        assert overrides["system.ai.claude-opus-4-8"]["cacheWrite"] == 10.0
        assert overrides["system.ai.claude-haiku-4-5"]["cacheWrite"] == 1.0

    def test_skips_models_without_usd_or_without_a_required_field(self):
        assert (
            pricing.usd_pricing_overrides(
                {
                    "system.ai.dbu-only": {
                        "DBU": {"input": 20.0, "output": 62.86, "cache_read": 3.71}
                    },
                    "system.ai.no-cache-read": {
                        "USD": {"input": 1.4, "output": 4.4, "cache_creation": 1.4}
                    },
                }
            )
            == {}
        )

    def test_empty_rates_build_no_rows(self):
        assert pricing.usd_pricing_overrides({}) == {}


class TestRefreshModelPricing:
    def _patch_fetch(self, monkeypatch, rates):
        monkeypatch.setattr(pricing, "get_databricks_token", lambda *a, **kw: "workspace-token")

        def fake_fetch(workspace, token, fqns):
            seen.append((workspace, token, fqns))
            return rates, None

        seen: list = []
        monkeypatch.setattr(pricing, "fetch_model_service_rates", fake_fetch)
        return seen

    def test_enabled_fetches_and_caches_pricing_lines(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")
        seen = self._patch_fetch(monkeypatch, GLM_RATES)
        state = {"workspace": WS}

        pricing.refresh_model_pricing(state, ["system.ai.glm-5-3[1m]", "main.default.other"])

        # Only system.ai models are requested, with ug's own `[1m]` hint stripped.
        assert seen == [(WS, "workspace-token", ["system.ai.glm-5-3"])]
        assert state[pricing.MODEL_PRICING_STATE_KEY] == {
            "system.ai.glm-5-3": TestUsdPricingOverrides.GLM_OVERRIDES
        }

    def test_aliased_models_are_priced_under_both_keys(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")
        self._patch_fetch(monkeypatch, GLM_RATES)
        state = {"workspace": WS}
        # Gateway discovery lists MPS-wrapped services under a hashed alias; a session on one
        # sends the alias verbatim, so the row must be keyed by it and by the canonical FQN.
        alias = "anthropic-aigw-982d9f93-system.ai.glm-5-3"

        pricing.refresh_model_pricing(state, [alias])

        assert state[pricing.MODEL_PRICING_STATE_KEY] == {
            alias: TestUsdPricingOverrides.GLM_OVERRIDES,
            "system.ai.glm-5-3": TestUsdPricingOverrides.GLM_OVERRIDES,
        }

    def test_failed_fetch_clears_the_cache_and_warns(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")
        monkeypatch.setattr(pricing, "get_databricks_token", lambda *a, **kw: "workspace-token")
        monkeypatch.setattr(
            pricing, "fetch_model_service_rates", lambda *a, **kw: ({}, "HTTP 404 Not Found")
        )
        warnings = []
        monkeypatch.setattr(pricing, "print_warning", lambda message: warnings.append(message))
        # A stale cache from an earlier configure must not outlive the failed refresh: a launch
        # renders whatever is cached, so its file would otherwise differ from this unpriced write.
        state = {"workspace": WS, pricing.MODEL_PRICING_STATE_KEY: {"system.ai.glm-5-3": "$1.40"}}

        assert pricing.refresh_model_pricing(state, ["system.ai.glm-5-3"]) == {}

        assert warnings == ["Model prices are unavailable for the model picker: HTTP 404 Not Found"]
        assert state[pricing.MODEL_PRICING_STATE_KEY] == {}

    def test_token_fetch_failure_leaves_the_picker_unpriced(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")

        def unauthenticated(*args, **kwargs):
            raise RuntimeError("Databricks CLI is not authenticated")

        monkeypatch.setattr(pricing, "get_databricks_token", unauthenticated)
        warnings = []
        monkeypatch.setattr(pricing, "print_warning", lambda message: warnings.append(message))
        state = {"workspace": WS}

        assert pricing.refresh_model_pricing(state, ["system.ai.glm-5-3"]) == {}

        assert warnings == [
            "Model prices are unavailable for the model picker: Databricks CLI is not authenticated"
        ]
        assert state[pricing.MODEL_PRICING_STATE_KEY] == {}

    def test_flag_off_skips_the_fetch_and_every_cache_write(self, monkeypatch):
        monkeypatch.setattr(
            pricing, "get_databricks_token", lambda *a, **kw: pytest.fail("must not authenticate")
        )
        monkeypatch.setattr(
            pricing,
            "fetch_model_service_rates",
            lambda *a, **kw: pytest.fail("must not call the rates API"),
        )
        state = {"workspace": WS, pricing.MODEL_PRICING_STATE_KEY: {"system.ai.glm-5-3": "$1.40"}}

        assert pricing.refresh_model_pricing(state, ["system.ai.glm-5-3"]) == {}

        # No fetch, and the existing cache is left untouched rather than cleared.
        assert state[pricing.MODEL_PRICING_STATE_KEY] == {"system.ai.glm-5-3": "$1.40"}

    def test_non_system_ai_models_fetch_nothing(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")
        seen = self._patch_fetch(monkeypatch, {})
        state = {"workspace": WS}

        assert pricing.refresh_model_pricing(state, ["main.default.claude-opus"]) == {}

        assert seen == []
        assert pricing.MODEL_PRICING_STATE_KEY not in state


class TestCachedModelPricing:
    def test_enabled_returns_the_cache(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")
        state = {pricing.MODEL_PRICING_STATE_KEY: {"system.ai.glm-5-3": "$1.40 input"}}

        assert pricing.cached_model_pricing(state) == {"system.ai.glm-5-3": "$1.40 input"}

    def test_flag_off_returns_none_even_with_a_cache(self, monkeypatch):
        # Prices cached by an earlier flag-on run must not reach a picker while off.
        state = {pricing.MODEL_PRICING_STATE_KEY: {"system.ai.glm-5-3": "$1.40 input"}}

        assert pricing.cached_model_pricing(state) is None

    def test_empty_cache_reads_as_none(self, monkeypatch):
        monkeypatch.setenv(pricing.MODEL_PRICING_ENV, "1")

        assert pricing.cached_model_pricing({}) is None
