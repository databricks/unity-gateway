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
    pricing.write_price_cache(path, PRICES)
    return path


def entry(
    agent: str,
    baseline: str | None,
    served: str,
    usage: dict,
    *,
    before_user_switch: bool = False,
    routed: bool = True,
) -> dict:
    found = {
        "agent": agent,
        "routed": routed if agent != "main" else False,
        "served": served,
        "before_user_switch": before_user_switch,
        "usage": usage,
    }
    return found if baseline is None else {**found, "baseline": baseline}


def write_usage(tmp_path: Path, entries: list, *, start_model: str | None = OPUS_ID) -> Path:
    path = tmp_path / savings.USAGE_FILENAME
    path.write_text(
        json.dumps({"version": 2, "start_model": start_model, "entries": entries}),
        encoding="utf-8",
    )
    return path


def saved(amount: str, percent: str) -> dict:
    return {"saved": True, "amount": amount, "percent": percent}


def cost_more(amount: str, percent: str) -> dict:
    return {"saved": False, "amount": amount, "percent": percent}


def decoded_savings(line: str | bytes) -> dict | None:
    output = json.loads(line)
    if output["savings"] is None:
        assert output == {"savings": None}
    else:
        assert set(output) == {"savings", "valid_for_ms"}
        assert type(output["valid_for_ms"]) is int
        assert 0 < output["valid_for_ms"] <= 24 * 60 * 60 * 1000
    return output["savings"]


def priced(usage: Path, price_cache: Path, *, first_prompt: bool = False) -> dict | None:
    line = savings.usage_output(usage, price_cache=price_cache, baseline_session_start=first_prompt)
    assert "\n" not in line
    return decoded_savings(line)


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

    @pytest.mark.parametrize(
        ("served", "split"),
        [
            ("anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra", None),
            ("anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra[1m]", {}),
            (
                "anthropic-aigw-12e8d1ee-system.ai.gpt-5-6-sol",
                {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
            ),
        ],
    )
    def test_translated_gpt_cache_writes_use_base_rates(self, tmp_path, price_cache, served, split):
        rates = ModelPrice(input=Decimal("1"), output=Decimal("5"), cache_write_5m=Decimal("1.25"))
        pricing.write_price_cache(price_cache, {served: rates})
        counts = {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 20_000,
            "output_tokens": 4_000,
        }
        if split is not None:
            counts["cache_creation"] = split
        path = write_usage(tmp_path, [entry("a1", OPUS_ID, served, counts)])

        # The same base-write token class costs $0.125 on Opus and $0.025 on GPT.
        assert savings.usage_savings(path, price_cache=price_cache, baseline_session_start=False)[
            :2
        ] == (Decimal("0.184"), Decimal("0.23"))

    def test_translated_gpt_cache_writes_keep_splits_and_require_used_rates(
        self, tmp_path, price_cache
    ):
        served = "anthropic-aigw-bf79c578-system.ai.gpt-6-1-terra[1m]"
        counts = {
            "cache_creation_input_tokens": 20_000,
            "cache_creation": {
                "ephemeral_1h_input_tokens": 5_000,
                "ephemeral_5m_input_tokens": 10_000,
            },
        }
        path = write_usage(tmp_path, [entry("a1", OPUS_ID, served, counts)])
        rates = ModelPrice(input=Decimal("1"), cache_write_5m=Decimal("1.25"))
        pricing.write_price_cache(price_cache, {served: rates})
        assert priced(path, price_cache) is None  # The explicit 1h tokens remain unpriced.

        rates = ModelPrice(
            input=Decimal("1"), cache_write_5m=Decimal("1.25"), cache_write_1h=Decimal("2")
        )
        pricing.write_price_cache(price_cache, {served: rates})
        assert savings.usage_savings(path, price_cache=price_cache, baseline_session_start=False)[
            :2
        ] == (Decimal("0.115"), Decimal("0.14375"))
        pricing.write_price_cache(
            price_cache, {served: ModelPrice(input=Decimal("1"), cache_write_1h=Decimal("2"))}
        )
        assert (
            priced(path, price_cache) is None
        )  # Neither input nor 1h rates replace missing base rates.

    @pytest.mark.parametrize(
        "served",
        [
            "system.ai.gpt-6-1-terra",
            "anthropic-aigw-00000000-system.ai.gpt-6-1-terra",
            "anthropic-aigw-77df06ea-system.ai.glm-5-3-flash",
            "anthropic-aigw-01234567-custom-gpt-model",
        ],
    )
    def test_unrecognized_cache_write_models_keep_conservative_pricing(
        self, tmp_path, price_cache, served
    ):
        pricing.write_price_cache(
            price_cache, {served: ModelPrice(input=Decimal("1"), cache_write_5m=Decimal("1.25"))}
        )
        path = write_usage(
            tmp_path, [entry("a1", OPUS_ID, served, {"cache_creation_input_tokens": 20_000})]
        )
        assert priced(path, price_cache) is None

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
        entries = [
            entry("main", OPUS_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
        ]
        usage = write_usage(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.38005 at Opus vs actual $0.15202 on Sonnet.
        assert priced(usage, price_cache, first_prompt=True) == saved("$0.23", "60%")
        assert priced(usage, price_cache) == saved("$0.23", "60%")

    def test_first_prompt_routing_prices_entries_after_a_user_switch_as_their_own(
        self, tmp_path, price_cache
    ):
        entries = [
            entry("main", OPUS_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
            entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
            # The user's `/model opus`, then `/model sonnet`.
            entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE),
            entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE),
        ]
        usage = write_usage(tmp_path, entries, start_model=OPUS_ID)

        # Baseline $0.48512 - actual $0.25709.
        assert priced(usage, price_cache, first_prompt=True) == saved("$0.23", "47%")

    @pytest.mark.parametrize("start_model", [None, ""])
    def test_legacy_first_prompt_routing_without_a_start_model_has_no_estimate(
        self, tmp_path, price_cache, start_model
    ):
        entries = [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True)]
        usage = write_usage(tmp_path, entries, start_model=start_model)
        document = json.loads(usage.read_text())
        document["version"] = 1
        usage.write_text(json.dumps(document))

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

    @pytest.mark.parametrize("first_prompt", [False, True])
    def test_incomplete_usage_hides_previously_positive_totals(
        self, tmp_path, price_cache, first_prompt
    ):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        assert priced(usage, price_cache, first_prompt=first_prompt) == saved("$0.18", "60%")
        document = json.loads(usage.read_text(encoding="utf-8"))
        document["incomplete_usage"] = True
        usage.write_text(json.dumps(document), encoding="utf-8")

        assert priced(usage, price_cache, first_prompt=first_prompt) is None

    def test_prices_a_seven_digit_gateway_alias(self, tmp_path, price_cache):
        usage = write_usage(
            tmp_path,
            [entry("a1", OPUS_ID, "anthropic-aigw-1a2b3c4-claude-sonnet-5", SUBAGENT_USAGE)],
        )

        assert priced(usage, price_cache) == saved("$0.18", "60%")

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


@pytest.mark.parametrize("first_prompt", [False, True])
def test_unrouted_children_add_cost_without_attributed_savings(tmp_path, price_cache, first_prompt):
    entries = [
        entry("main", OPUS_ID, OPUS_ID, MAIN_USAGE, before_user_switch=True),
        entry("routed", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True),
        entry(
            "unrouted", SONNET_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True, routed=False
        ),
    ]
    usage = write_usage(tmp_path, entries)
    computed = savings.usage_savings(
        usage, price_cache=price_cache, baseline_session_start=first_prompt
    )
    assert computed is not None
    assert computed[:2] == (Decimal("0.183"), Decimal("0.50205"))
    assert priced(usage, price_cache, first_prompt=first_prompt) == saved("$0.18", "36%")


@pytest.mark.parametrize("first_prompt", [False, True])
def test_unrouted_children_never_use_a_main_model_baseline(tmp_path, price_cache, first_prompt):
    usage = write_usage(
        tmp_path,
        [
            entry(
                "pinned", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True, routed=False
            ),
        ],
    )
    assert priced(usage, price_cache, first_prompt=first_prompt) is None


@pytest.mark.parametrize("attribution", [None, "true", 1])
@pytest.mark.parametrize("first_prompt", [False, True])
def test_unknown_child_attribution_hides_totals_without_discarding_usage(
    tmp_path, price_cache, attribution, first_prompt
):
    legacy = entry("legacy", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True)
    if attribution is None:
        legacy.pop("routed")
    else:
        legacy["routed"] = attribution
    usage = write_usage(tmp_path, [entry("known", OPUS_ID, SONNET_ID, SUBAGENT_USAGE), legacy])
    original = usage.read_bytes()
    assert priced(usage, price_cache, first_prompt=first_prompt) is None
    assert usage.read_bytes() == original


class TestSavingsSegment:
    def test_rounds_to_cents_and_whole_percent(self):
        assert savings.savings_segment(Decimal("1234.565"), Decimal("2469.13")) == saved(
            "$1,234.57", "50%"
        )

    def test_negative_reads_as_a_cost_increase(self):
        assert savings.savings_segment(Decimal("-0.04"), Decimal("0.40")) == cost_more(
            "$0.04", "10%"
        )

    @pytest.mark.parametrize("value,amount", [("0.004", "<$0.01"), ("0", "$0.00")])
    def test_formats_sub_cent_and_zero_amounts(self, value, amount):
        assert savings.savings_segment(Decimal(value), Decimal("1")) == saved(amount, "0%")


class TestPricerCommand:
    @staticmethod
    def run(
        price_cache: Path,
        usage: Path,
        *,
        first_prompt: bool = False,
        flag: str | None = "1",
        controls: Path | None = None,
        snapshot: bytes | None = None,
        no_price: bool = False,
    ):
        argv = savings.pricer_argv(
            python=sys.executable, price_cache=price_cache, baseline_session_start=first_prompt
        )
        env = {**os.environ, "PYTHONIOENCODING": "ascii"}
        env.pop("ENABLE_SMART_ROUTING_SAVINGS", None)
        env.pop("UCODE_SESSION_ENV_FILE", None)
        if controls is not None:
            env["UCODE_SESSION_ENV_FILE"] = str(controls)
        if flag is not None:
            env["ENABLE_SMART_ROUTING_SAVINGS"] = flag
        if snapshot is not None:
            argv.append("--write-usage")
        if no_price:
            argv.append("--no-price")
        # The line must survive an ASCII stdout locale.
        return subprocess.run(
            [*argv, "--usage", str(usage)],
            capture_output=True,
            env=env,
            input=snapshot,
            timeout=30,
            check=False,
        )

    @pytest.mark.parametrize("persist", [False, True])
    def test_prints_exactly_one_json_line(self, tmp_path, price_cache, persist):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        snapshot = usage.read_bytes() if persist else None
        if persist:
            usage.write_text('{"version":1,"entries":[]}', encoding="utf-8")

        result = self.run(price_cache, usage, snapshot=snapshot)

        assert result.returncode == 0, result.stderr
        assert result.stdout.count(b"\n") == 1
        assert decoded_savings(result.stdout.decode("utf-8")) == saved("$0.18", "60%")
        if snapshot is not None:
            assert json.loads(usage.read_bytes()) == json.loads(snapshot)

    @pytest.mark.parametrize("snapshot", [b"invalid", b"[]", b'{"version":999,"entries":[]}'])
    def test_rejected_snapshot_preserves_usage(self, tmp_path, price_cache, snapshot):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        previous = usage.read_bytes()

        result = self.run(price_cache, usage, snapshot=snapshot)

        assert result.returncode == 1
        assert json.loads(result.stdout) == {"savings": None}
        assert usage.read_bytes() == previous

    def test_save_only_preserves_usage_without_computing_an_estimate(self, tmp_path, price_cache):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        snapshot = usage.read_bytes()
        usage.unlink()

        result = self.run(price_cache, usage, snapshot=snapshot, no_price=True)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"savings": None}
        assert json.loads(usage.read_bytes()) == json.loads(snapshot)

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

    @pytest.mark.parametrize("persist", [False, True])
    def test_pricer_obeys_session_controls(self, tmp_path, price_cache, persist):
        usage = write_usage(tmp_path, [entry("a1", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
        snapshot = usage.read_bytes() if persist else None
        if persist:
            usage.write_text('{"version":1,"entries":[]}', encoding="utf-8")
        controls = tmp_path / "session-env.json"
        for overrides, expected in (
            ({"ENABLE_SMART_ROUTING_SAVINGS": "0"}, None),
            ({}, saved("$0.18", "60%")),
        ):
            controls.write_text(json.dumps(overrides))
            result = self.run(price_cache, usage, controls=controls, snapshot=snapshot)
            assert result.returncode == 0, result.stderr
            assert decoded_savings(result.stdout) == expected
            if snapshot is not None:
                assert json.loads(usage.read_bytes()) == json.loads(snapshot)

        controls.write_text("invalid JSON")
        fallback = self.run(price_cache, usage, controls=controls)
        assert fallback.returncode == 0, fallback.stderr
        assert decoded_savings(fallback.stdout) == saved("$0.18", "60%")
        assert fallback.stderr == b""


@pytest.mark.parametrize("earlier_model", [OPUS_ID, SONNET_ID])
def test_estimate_validity_uses_only_the_models_it_prices(tmp_path, monkeypatch, earlier_model):
    clock = [100.0]
    monkeypatch.setattr(pricing.time, "time", lambda: clock[0])
    cache = tmp_path / "prices.json"
    pricing.write_price_cache(cache, {"unused-model": PRICES[OPUS_ID]})
    clock[0] = 200.0
    pricing.write_price_cache(cache, {earlier_model: PRICES[earlier_model]})
    clock[0] = 300.0
    pricing.write_price_cache(
        cache, {key: value for key, value in PRICES.items() if key != earlier_model}
    )
    usage = write_usage(tmp_path, [entry("child", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
    original = usage.read_bytes()

    def output():
        return json.loads(
            savings.usage_output(usage, price_cache=cache, baseline_session_start=False)
        )

    assert output() == {"savings": saved("$0.18", "60%"), "valid_for_ms": 86_300_000}
    clock[0] = 200.0 + 24 * 60 * 60 - 1
    assert output() == {"savings": saved("$0.18", "60%"), "valid_for_ms": 1000}
    clock[0] += 1
    assert output() == {"savings": None}
    assert usage.read_bytes() == original

    clock[0] += 1
    pricing.write_price_cache(cache, {earlier_model: PRICES[earlier_model]})
    assert output() == {"savings": saved("$0.18", "60%"), "valid_for_ms": 99_000}
    assert usage.read_bytes() == original


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
    ],
)
@pytest.mark.parametrize("bad", [0.5, -1, float("inf"), float("nan"), True, False, None, "1"])
def test_malformed_saved_counts_hide_the_whole_estimate_and_preserve_snapshots(
    tmp_path, price_cache, field, bad
):
    malformed = {"input_tokens": 7}
    target = (
        malformed.setdefault("cache_creation", {}) if field.startswith("ephemeral_") else malformed
    )
    target[field] = bad
    usage = write_usage(
        tmp_path,
        [
            entry("known", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            entry("malformed", OPUS_ID, SONNET_ID, malformed),
        ],
    )
    invalid_bytes = usage.read_bytes()
    assert priced(usage, price_cache) is None
    assert usage.read_bytes() == invalid_bytes

    write_usage(tmp_path, [entry("known", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
    valid_bytes = usage.read_bytes()
    result = TestPricerCommand.run(price_cache, usage, snapshot=invalid_bytes)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"savings": None}
    assert usage.read_bytes() == valid_bytes


@pytest.mark.parametrize(
    "counts",
    [
        {"cache_creation_input_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 1000}},
        {
            "cache_creation_input_tokens": 2,
            "cache_creation": {"ephemeral_1h_input_tokens": 2, "ephemeral_5m_input_tokens": 1},
        },
        {"cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_5m_input_tokens": 1}},
    ],
)
def test_inconsistent_cache_splits_hide_the_estimate_and_preserve_snapshots(
    tmp_path, price_cache, counts
):
    usage = write_usage(tmp_path, [entry("child", OPUS_ID, SONNET_ID, counts)])
    inconsistent = usage.read_bytes()
    assert priced(usage, price_cache) is None
    assert usage.read_bytes() == inconsistent

    write_usage(tmp_path, [entry("known", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
    previous = usage.read_bytes()
    result = TestPricerCommand.run(price_cache, usage, snapshot=inconsistent)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"savings": None}
    assert usage.read_bytes() == previous


@pytest.mark.parametrize("total,amount", [(None, "$0.18"), (30_000, "$0.24")])
def test_partial_and_split_only_cache_breakdowns_remain_priceable(
    tmp_path, price_cache, total, amount
):
    counts = {
        key: value for key, value in SUBAGENT_USAGE.items() if key != "cache_creation_input_tokens"
    }
    if total is not None:
        counts["cache_creation_input_tokens"] = total
    usage = write_usage(tmp_path, [entry("child", OPUS_ID, SONNET_ID, counts)])
    snapshot = usage.read_bytes()
    result = TestPricerCommand.run(price_cache, usage, snapshot=snapshot)
    assert result.returncode == 0, result.stderr
    assert decoded_savings(result.stdout) == saved(amount, "60%")
    assert json.loads(usage.read_bytes()) == json.loads(snapshot)


def test_zero_counts_and_absent_optional_counts_remain_priceable(tmp_path, price_cache):
    usage = write_usage(
        tmp_path,
        [
            entry("known", OPUS_ID, SONNET_ID, SUBAGENT_USAGE),
            entry(
                "zero",
                "unpriced",
                "unpriced",
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                },
            ),
            entry("empty", "unpriced", "unpriced", {}),
        ],
    )
    snapshot = usage.read_bytes()
    result = TestPricerCommand.run(price_cache, usage, snapshot=snapshot)
    assert result.returncode == 0, result.stderr
    assert decoded_savings(result.stdout) == saved("$0.18", "60%")
    assert json.loads(usage.read_bytes()) == json.loads(snapshot)


@pytest.mark.parametrize("first_prompt", [False, True])
def test_legacy_ambiguous_baseline_does_not_depend_on_launch_mode(
    tmp_path, price_cache, first_prompt
):
    usage = write_usage(
        tmp_path, [entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True)]
    )
    document = json.loads(usage.read_text())
    document["version"] = 1
    usage.write_text(json.dumps(document))
    before = usage.read_bytes()
    assert priced(usage, price_cache, first_prompt=first_prompt) is None
    assert usage.read_bytes() == before


@pytest.mark.parametrize("first_prompt", [False, True])
@pytest.mark.parametrize(
    "agent,routed,before,baseline",
    [
        ("main", False, True, OPUS_ID),
        ("main", False, False, SONNET_ID),
        ("child", False, True, SONNET_ID),
    ],
)
def test_legacy_unambiguous_baseline_remains_priceable(
    tmp_path, price_cache, first_prompt, agent, routed, before, baseline
):
    historical = entry(agent, baseline, SONNET_ID, MAIN_USAGE, before_user_switch=before)
    historical["routed"] = routed
    usage = write_usage(
        tmp_path,
        [historical, entry("routed", OPUS_ID, SONNET_ID, SUBAGENT_USAGE, before_user_switch=True)],
    )
    document = json.loads(usage.read_text())
    document["version"] = 1
    usage.write_text(json.dumps(document))
    assert priced(usage, price_cache, first_prompt=first_prompt) is not None
    assert priced(usage, price_cache, first_prompt=first_prompt) == priced(
        usage, price_cache, first_prompt=not first_prompt
    )


@pytest.mark.parametrize("first_prompt", [False, True])
def test_resumed_presets_keep_each_recorded_baseline(tmp_path, price_cache, first_prompt):
    entries = [
        entry("main", OPUS_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
        entry("main", SONNET_ID, SONNET_ID, MAIN_USAGE, before_user_switch=True),
    ]
    usage = write_usage(tmp_path, entries)
    expected = priced(usage, price_cache, first_prompt=first_prompt)
    assert expected is not None
    assert priced(usage, price_cache, first_prompt=not first_prompt) == expected
    document = json.loads(usage.read_text())
    document["entries"].append(entry("child", SONNET_ID, OPUS_ID, SUBAGENT_USAGE))
    usage.write_text(json.dumps(document))
    assert priced(usage, price_cache, first_prompt=first_prompt) == priced(
        usage, price_cache, first_prompt=not first_prompt
    )


@pytest.mark.parametrize("pending", [1, True, -1, 0.5])
def test_pending_or_malformed_request_counts_hide_the_estimate(tmp_path, price_cache, pending):
    usage = write_usage(tmp_path, [entry("child", OPUS_ID, SONNET_ID, SUBAGENT_USAGE)])
    document = json.loads(usage.read_text())
    document["pending_steps"] = pending
    usage.write_text(json.dumps(document))
    assert priced(usage, price_cache) is None


def test_native_session_storage_is_durable_and_workspace_scoped(tmp_path):
    from ucode.workspace import workspace_key

    host = "https://oregon.cloud.databricks.com"
    first = savings.usage_directory(tmp_path, workspace_key(host, "111"))
    assert first == savings.usage_directory(tmp_path, workspace_key(host.upper(), "111"))
    assert first != savings.usage_directory(tmp_path, workspace_key(host, "222"))
    assert first != savings.usage_directory(tmp_path, "https://other.cloud.databricks.com")
