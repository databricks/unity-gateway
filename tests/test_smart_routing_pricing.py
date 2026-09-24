"""Tests for per-token model pricing used by the smart-routing savings statusline."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from ucode.smart_routing import pricing
from ucode.smart_routing.pricing import ModelPrice, TokenUsage

# Catalog list prices (USD per million tokens) for the two Claude route arms.
OPUS = ModelPrice(
    input=Decimal("5"),
    output=Decimal("25"),
    cache_read=Decimal("0.5"),
    cache_write_5m=Decimal("6.25"),
    cache_write_1h=Decimal("10"),
)
SONNET_4 = ModelPrice(
    input=Decimal("3"),
    output=Decimal("15"),
    cache_read=Decimal("0.3"),
    cache_write_5m=Decimal("3.75"),
    long_context_threshold=200_000,
    long_context=ModelPrice(
        input=Decimal("6"),
        output=Decimal("22.5"),
        cache_read=Decimal("0.6"),
        cache_write_5m=Decimal("7.5"),
    ),
)


class TestModelKey:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-8",
            "system.ai.claude-opus-4-8",
            "system.ai.claude-opus-4-8[1m]",
            "databricks-claude-opus-4-8",
            "anthropic-aigw-1a2b3c4d-claude-opus-4-8",
            "SYSTEM.AI.Claude-Opus-4-8",
        ],
    )
    def test_collapses_gateway_spellings(self, model):
        assert pricing.model_key(model) == "claude-opus-4-8"

    def test_keeps_distinct_models_distinct(self):
        assert pricing.model_key("system.ai.claude-sonnet-5") != pricing.model_key(
            "system.ai.claude-opus-4-8"
        )


class TestTokenUsage:
    def test_splits_cache_writes_by_ttl(self):
        tokens = TokenUsage.from_message_usage(
            {
                "input_tokens": 2,
                "cache_creation_input_tokens": 6509,
                "cache_read_input_tokens": 143654,
                "output_tokens": 695,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 6000,
                    "ephemeral_5m_input_tokens": 9,
                },
            }
        )

        # The 500 writes the breakdown doesn't cover bill at the default 5-minute TTL.
        assert tokens == TokenUsage(
            input=2, cache_write_5m=509, cache_write_1h=6000, cache_read=143654, output=695
        )
        assert tokens.prompt_tokens == 2 + 6509 + 143654

    def test_treats_missing_breakdown_as_five_minute_writes(self):
        tokens = TokenUsage.from_message_usage({"cache_creation_input_tokens": 40})

        assert tokens == TokenUsage(cache_write_5m=40)

    @pytest.mark.parametrize("bad", [None, -5, True, "12", 1.5])
    def test_ignores_non_count_values(self, bad):
        assert TokenUsage.from_message_usage({"input_tokens": bad, "output_tokens": 3}) == (
            TokenUsage(output=3)
        )


class TestTokenCost:
    def test_prices_every_token_class_at_its_own_rate(self):
        tokens = TokenUsage(
            input=1_000, cache_write_5m=2_000, cache_write_1h=3_000, cache_read=100_000, output=500
        )

        # 1k*5 + 2k*6.25 + 3k*10 + 100k*0.5 + 500*25 = 110,000 micro-dollars.
        assert pricing.token_cost(OPUS, tokens) == Decimal("0.11")

    def test_uses_long_context_rates_above_the_threshold(self):
        short = TokenUsage(input=100_000, output=1_000)
        long = TokenUsage(input=150_000, cache_read=60_000, output=1_000)

        assert pricing.token_cost(SONNET_4, short) == Decimal("0.315")
        assert pricing.token_cost(SONNET_4, long) == Decimal("0.9585")

    def test_returns_none_when_a_used_class_has_no_rate(self):
        # SONNET_4 publishes no 1-hour write rate.
        assert pricing.token_cost(SONNET_4, TokenUsage(cache_write_1h=10)) is None

    def test_missing_rate_for_an_unused_class_is_fine(self):
        assert pricing.token_cost(SONNET_4, TokenUsage(input=100_000)) == Decimal("0.3")


class TestPriceCache:
    def test_round_trips_prices_under_model_keys(self, tmp_path):
        path = tmp_path / "cache" / pricing.PRICE_CACHE_FILENAME
        pricing.write_price_cache(
            path, {"system.ai.claude-opus-4-8": OPUS, "claude-sonnet-4": SONNET_4}, now=100.0
        )

        cached = pricing.read_price_cache(path)

        assert cached is not None
        prices, fingerprint = cached
        assert prices == {"claude-opus-4-8": OPUS, "claude-sonnet-4": SONNET_4}
        assert fingerprint == "100.0"
        assert [entry.name for entry in path.parent.iterdir()] == [pricing.PRICE_CACHE_FILENAME]

    def test_fingerprint_changes_on_refresh(self, tmp_path):
        path = tmp_path / pricing.PRICE_CACHE_FILENAME
        pricing.write_price_cache(path, {"claude-opus-4-8": OPUS}, now=100.0)
        first = pricing.read_price_cache(path)
        pricing.write_price_cache(path, {"claude-opus-4-8": OPUS}, now=200.0)

        assert first is not None and pricing.read_price_cache(path) is not None
        assert first[1] != pricing.read_price_cache(path)[1]

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            json.dumps({"version": 999, "models": {"claude-opus-4-8": {"input": "5"}}}),
            json.dumps({"version": 1, "models": {}}),
            json.dumps({"version": 1, "models": []}),
        ],
    )
    def test_unusable_cache_reads_as_absent(self, tmp_path, content):
        path = tmp_path / pricing.PRICE_CACHE_FILENAME
        path.write_text(content)

        assert pricing.read_price_cache(path) is None

    def test_missing_cache_reads_as_absent(self, tmp_path):
        assert pricing.read_price_cache(tmp_path / "missing.json") is None
