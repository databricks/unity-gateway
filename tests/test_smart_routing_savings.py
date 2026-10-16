"""Tests for pricing the Claude mod's token sums into the smart-routing savings estimate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from ucode.smart_routing import pricing, savings
from ucode.smart_routing.pricing import ModelPrice

OPUS_ID = "system.ai.claude-opus-4-8"
SONNET_ID = "system.ai.claude-sonnet-5"
PRICES = {
    OPUS_ID: ModelPrice(
        input=Decimal("5"),
        output=Decimal("25"),
        cache_read=Decimal("0.5"),
        cache_write_5m=Decimal("6.25"),
        cache_write_1h=Decimal("10"),
    ),
    SONNET_ID: ModelPrice(
        input=Decimal("2"),
        output=Decimal("10"),
        cache_read=Decimal("0.2"),
        cache_write_5m=Decimal("2.5"),
        cache_write_1h=Decimal("4"),
    ),
}
# Main-agent request: $0.07505 on Opus (10*5 + 100k*0.5 + 1k*25), $0.03002 on Sonnet.
MAIN_USAGE = {"input_tokens": 10, "cache_read_input_tokens": 100_000, "output_tokens": 1_000}
# Subagent request: $0.122 on Sonnet (1k*2 + 20k*4 + 4k*10), $0.305 at Opus rates.
SUBAGENT_USAGE = {
    "input_tokens": 1_000,
    "cache_creation_input_tokens": 20_000,
    "cache_creation": {"ephemeral_1h_input_tokens": 20_000, "ephemeral_5m_input_tokens": 0},
    "output_tokens": 4_000,
}


@pytest.fixture
def price_cache(tmp_path) -> Path:
    path = tmp_path / "model-prices.json"
    pricing.write_price_cache(path, PRICES, now=1.0)
    return path


def entry(
    agent: str, baseline: str | None, served: str, usage: dict, *, before_user_switch: bool = False
) -> dict:
    found = {
        "agent": agent,
        "served": served,
        "before_user_switch": before_user_switch,
        "usage": usage,
    }
    return found if baseline is None else {**found, "baseline": baseline}


def write_usage(tmp_path: Path, entries: list, *, start_model: str | None = OPUS_ID) -> Path:
    path = tmp_path / savings.USAGE_FILENAME
    path.write_text(
        json.dumps({"version": 1, "start_model": start_model, "entries": entries}),
        encoding="utf-8",
    )
    return path


def saved(amount: str, percent: str) -> dict:
    return {"saved": True, "amount": amount, "percent": percent}


def cost_more(amount: str, percent: str) -> dict:
    return {"saved": False, "amount": amount, "percent": percent}


def priced(usage: Path, price_cache: Path, *, first_prompt: bool = False) -> dict | None:
    line = savings.usage_output(usage, price_cache=price_cache, baseline_session_start=first_prompt)
    assert "\n" not in line
    return json.loads(line)["savings"]


class TestUsageSavings:
    def test_prices_tokens_against_each_entrys_baseline(self, tmp_path, price_cache):
        entries = [
            entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
        ]

        # Baseline $0.38005 - actual $0.19705.
        assert priced(write_usage(tmp_path, entries), price_cache) == saved("$0.18", "48%")

    def test_an_entrys_baseline_is_the_main_model_when_its_request_ran(self, tmp_path, price_cache):
        entries = [
            entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            # After the user's `/model sonnet`, subagents on Sonnet saved nothing.
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
            entry("a1", SONNET_ID, SONNET_ID, SUBAGENT_USAGE),
        ]

        assert priced(write_usage(tmp_path, entries), price_cache) == saved("$0.18", "34%")

    def test_shows_a_net_cost_increase_honestly(self, tmp_path, price_cache):
        entries = [
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
            entry("a1", SONNET_ID, OPUS_ID, SUBAGENT_USAGE),
        ]

        # Baseline $0.15202 vs actual $0.33502.
        assert priced(write_usage(tmp_path, entries), price_cache) == cost_more("$0.18", "120%")

    def test_nothing_to_claim_until_a_model_was_swapped(self, tmp_path, price_cache):
        entries = [entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE)]

        assert priced(write_usage(tmp_path, entries), price_cache) is None

    def test_bills_entries_without_the_cache_write_split_as_one_hour_writes(
        self, tmp_path, price_cache
    ):
        usage = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "output_tokens": 4_000,
        }

        # ug launches Claude with 1-hour caching, so unsplit writes bill at the 1-hour rate, as
        # SUBAGENT_USAGE does: $0.122 on Sonnet vs $0.305 at Opus rates.
        assert priced(
            write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, usage)]), price_cache
        ) == saved("$0.18", "60%")

    def test_honors_an_explicit_cache_write_split(self, tmp_path, price_cache):
        usage = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 20_000},
            "output_tokens": 4_000,
        }

        # The split says 5-minute writes: $0.092 on Sonnet vs $0.23 at Opus rates.
        assert priced(
            write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, usage)]), price_cache
        ) == saved("$0.14", "60%")

    def test_skips_entries_without_tokens(self, tmp_path, price_cache):
        entries = [
            entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            entry("a2", "unpriced-baseline", "unpriced-served", {"input_tokens": 0}),
        ]

        assert priced(write_usage(tmp_path, entries), price_cache) == saved("$0.18", "60%")

    def test_first_prompt_routing_prices_against_the_start_model(self, tmp_path, price_cache):
        # Under first-prompt routing the router picks the main model too, so an entry's own
        # baseline (the routed model) would hide the saving; until the user switches the main
        # model its presence or absence is ignored.
        entries = [
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            entry("a1", None, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
        ]
        usage = write_usage(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.38005 at Opus vs actual $0.15202 on Sonnet.
        assert priced(usage, price_cache, first_prompt=True) == saved("$0.23", "60%")
        assert priced(usage, price_cache) is None

    def test_first_prompt_routing_prices_entries_after_a_user_switch_as_their_own(
        self, tmp_path, price_cache
    ):
        entries = [
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            entry("a1", SONNET_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
            # The user's `/model opus`, then `/model sonnet`.
            entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
        ]
        usage = write_usage(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.48512 - actual $0.25709.
        assert priced(usage, price_cache, first_prompt=True) == saved("$0.23", "47%")

    @pytest.mark.parametrize("start_model", [None, ""])
    def test_first_prompt_routing_without_a_start_model_has_no_estimate(
        self, tmp_path, price_cache, start_model
    ):
        entries = [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]
        usage = write_usage(tmp_path, entries, start_model=start_model)

        assert priced(usage, price_cache, first_prompt=True) is None

    @pytest.mark.parametrize(
        ("baseline", "served"),
        [
            # An unpriced served model, or an unpriced baseline, would undercount.
            (OPUS_ID, "system.ai.claude-mystery-1"),
            ("system.ai.claude-mystery-1", SONNET_ID),
            # Baseline-less or model-less entries.
            (None, SONNET_ID),
            ("", SONNET_ID),
            (OPUS_ID, ""),
        ],
    )
    def test_an_unpriced_or_baseline_less_entry_voids_the_estimate(
        self, tmp_path, price_cache, baseline, served
    ):
        entries = [
            entry("a0", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            entry("a1", baseline, served, SUBAGENT_USAGE),
        ]

        assert priced(write_usage(tmp_path, entries), price_cache) is None

    @pytest.mark.parametrize(
        "bad", ["nope", 7, {"baseline": OPUS_ID, "served": SONNET_ID}, {"baseline": OPUS_ID}]
    )
    def test_a_malformed_entry_voids_the_estimate(self, tmp_path, price_cache, bad):
        entries = [entry("a0", OPUS_ID, SONNET_ID, SUBAGENT_USAGE), bad]

        assert priced(write_usage(tmp_path, entries), price_cache) is None

    def test_without_prices_there_is_no_estimate(self, tmp_path, price_cache):
        price_cache.unlink()
        entries = [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)]

        assert priced(write_usage(tmp_path, entries), price_cache) is None

    @pytest.mark.parametrize(
        "text",
        [None, "not json", "[]", json.dumps({"version": 2, "entries": []}), '{"version": 1}'],
    )
    def test_an_unusable_file_has_no_estimate(self, tmp_path, price_cache, text):
        usage = tmp_path / savings.USAGE_FILENAME
        if text is not None:
            usage.write_text(text, encoding="utf-8")

        assert priced(usage, price_cache) is None

    def test_a_file_in_another_format_is_an_error_not_an_empty_estimate(
        self, tmp_path, price_cache
    ):
        usage = tmp_path / savings.USAGE_FILENAME
        usage.write_text(json.dumps({"version": 2, "entries": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="unsupported savings usage file"):
            savings.usage_savings(usage, price_cache=price_cache, baseline_session_start=False)


class TestSavingsSegment:
    def test_rounds_to_cents_and_whole_percent(self):
        assert savings.savings_segment(Decimal("1234.565"), Decimal("2469.13")) == saved(
            "$1,234.57", "50%"
        )

    def test_negative_reads_as_a_cost_increase(self):
        assert savings.savings_segment(Decimal("-0.04"), Decimal("0.40")) == cost_more(
            "$0.04", "10%"
        )

    def test_sub_cent_amounts_are_not_shown_as_zero(self):
        assert savings.savings_segment(Decimal("0.004"), Decimal("1")) == saved("<$0.01", "0%")


class TestPricerCommand:
    @pytest.mark.parametrize("first_prompt", [False, True])
    def test_runs_the_module_on_the_given_python(self, tmp_path, first_prompt):
        argv = savings.pricer_argv(
            python="/py", price_cache=tmp_path / "prices.json", baseline_session_start=first_prompt
        )

        assert argv == [
            "/py",
            "-P",
            "-m",
            savings.MODULE,
            "--price-cache",
            str(tmp_path / "prices.json"),
            *(["--baseline-session-start"] if first_prompt else []),
        ]

    @staticmethod
    def run(price_cache: Path, usage: Path, *, first_prompt: bool = False, flag: str | None = "1"):
        argv = savings.pricer_argv(
            python=sys.executable, price_cache=price_cache, baseline_session_start=first_prompt
        )
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        env.pop("ENABLE_SMART_ROUTING_SAVINGS", None)
        if flag is not None:
            env["ENABLE_SMART_ROUTING_SAVINGS"] = flag
        # The line must survive an ASCII stdout locale.
        return subprocess.run(
            [*argv, "--usage", str(usage)],
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )

    def test_prints_exactly_one_json_line(self, tmp_path, price_cache):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])

        result = self.run(price_cache, usage)

        assert result.returncode == 0, result.stderr
        assert result.stdout.count(b"\n") == 1
        assert json.loads(result.stdout.decode("utf-8")) == {"savings": saved("$0.18", "60%")}

    def test_never_fails_the_mod(self, tmp_path, price_cache):
        result = self.run(price_cache, tmp_path / "missing.json", first_prompt=True)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"savings": None}

    @pytest.mark.parametrize("flag", [None, "", "0", "true"])
    def test_disabled_pricer_returns_no_estimate(self, tmp_path, price_cache, flag):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        result = self.run(price_cache, usage, flag=flag)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"savings": None}
