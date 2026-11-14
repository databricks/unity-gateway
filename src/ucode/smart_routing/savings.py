"""Price the Claude mod's token sums into a smart-routing savings estimate.

The savings estimate assumes every token the session used, main agent and subagents alike, would
otherwise have run on the baseline main-agent model::

    saved = sum(tokens * price(baseline)) - sum(tokens * price(model that served them))

per token class. The mod (``agents/claude_mods/register.ts``) sums each turn request's usage by
(agent, baseline, served) and sends this module a snapshot after every turn; it persists the
snapshot atomically and prints one JSON
line with the segment the mod draws. A main-agent request is its own baseline, and a subagent's is
the main model in effect when it ran, so switching models with ``/model`` never reprices earlier
work. Under first-prompt routing, where the router also picks the main model, requests from before
the user changed the main model are priced against the model selected before submitting the first
prompt, so a manual selection before routing becomes the baseline. A negative
result (routing chose pricier models) is shown as a cost increase.

Keep imports lightweight: the mod runs this in a fresh process after every turn,
and the CLI's usual imports take about a second.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ucode.constants import AGENT_CLAUDE, ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR
from ucode.smart_routing.pricing import (
    ModelPrice,
    TokenUsage,
    model_key,
    read_price_cache,
    token_cost,
)
from ucode.smart_routing.session_env import effective_environment

MODULE = "ucode.smart_routing.savings"
# The file holding the mod's token sums, in the launch directory; the mod reads its path from
# the plugin options, so only the version is shared with it.
USAGE_FILENAME = "savings-usage.json"
_USAGE_VERSION = 1
_CENT = Decimal("0.01")


def pricer_argv(*, python: str, price_cache: Path, baseline_session_start: bool) -> list[str]:
    """The command the Claude mod runs (plus ``--usage PATH``) to price its token sums."""
    # -P keeps the launch directory off sys.path, so a project file can't shadow a stdlib module.
    argv = [python, "-P", "-m", MODULE, "--price-cache", str(price_cache)]
    if baseline_session_start:
        argv.append("--baseline-session-start")
    return argv


def _response_costs(
    prices: dict[str, ModelPrice], served_key: str, baseline_key: str, tokens: TokenUsage
) -> tuple[Decimal, Decimal] | None:
    """``(actual, baseline)`` dollars for one entry, or None when either model can't be priced."""
    served_price = prices.get(served_key)
    baseline_price = prices.get(baseline_key)
    if served_price is None or baseline_price is None:
        return None
    actual = token_cost(served_price, tokens)
    baseline = token_cost(baseline_price, tokens)
    if actual is None or baseline is None:
        return None
    return actual, baseline


def usage_savings(
    usage_path: Path, *, price_cache: Path, baseline_session_start: bool
) -> tuple[Decimal, Decimal] | None:
    """The ``(saved, baseline)`` dollars for the mod's token sums, or None with nothing to claim.

    None until routing changed some model, and whenever an entry is malformed, baseline-less or
    unpriced: an undercounted figure would be worse than none. A file that can't be read as the
    mod's format at all is an error, not an empty estimate.
    """
    document = json.loads(usage_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != _USAGE_VERSION:
        raise ValueError(f"unsupported savings usage file: {usage_path}")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"unsupported savings usage file: {usage_path}")
    fixed_baseline = None
    if baseline_session_start:
        start = document.get("start_model")
        if not isinstance(start, str) or not start:
            return None
        fixed_baseline = start
    prices = read_price_cache(price_cache)
    if prices is None:
        return None

    actual_total = baseline_total = Decimal(0)
    rerouted = False
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        usage, served = entry.get("usage"), entry.get("served")
        baseline = entry.get("baseline")
        if fixed_baseline is not None and entry.get("before_user_switch") is True:
            baseline = fixed_baseline
        if not isinstance(usage, dict) or not isinstance(served, str) or not served:
            return None
        if not isinstance(baseline, str) or not baseline:
            return None
        # The mod's usage has no cache-write TTL split, and ug launches Claude with 1-hour caching.
        tokens = TokenUsage.from_message_usage(usage, uncovered_writes_1h=True)
        if not any(tokens):
            continue
        served_key, baseline_key = model_key(served), model_key(baseline)
        costs = _response_costs(prices, served_key, baseline_key, tokens)
        if costs is None:
            return None
        actual_total += costs[0]
        baseline_total += costs[1]
        rerouted = rerouted or served_key != baseline_key
    if not rerouted or baseline_total <= 0:
        return None
    return baseline_total - actual_total, baseline_total


def savings_segment(saved: Decimal, baseline: Decimal) -> dict[str, object]:
    """The estimate's parts, for the mod to style: a saving, or an honest cost increase."""
    percent = (abs(saved) / baseline * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    magnitude = abs(saved)
    amount = (
        "<$0.01"
        if 0 < magnitude < _CENT
        else f"${magnitude.quantize(_CENT, rounding=ROUND_HALF_UP):,}"
    )
    return {"saved": saved >= 0, "amount": amount, "percent": f"{percent}%"}


def usage_output(usage_path: Path, *, price_cache: Path, baseline_session_start: bool) -> str:
    """The one JSON line the mod reads: its savings segment, or null."""
    # The mod must get a parseable line whatever goes wrong.
    savings = None
    try:
        computed = usage_savings(
            usage_path, price_cache=price_cache, baseline_session_start=baseline_session_start
        )
        savings = None if computed is None else savings_segment(*computed)
    except Exception:  # noqa: BLE001
        pass
    return json.dumps({"savings": savings})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=MODULE, description="Price the Claude mod's token sums as a JSON line."
    )
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument(
        "--write-usage", action="store_true", help="Save the JSON snapshot on stdin."
    )
    parser.add_argument("--price-cache", type=Path, required=True)
    parser.add_argument("--baseline-session-start", action="store_true")
    args = parser.parse_args(argv)
    enabled = (
        effective_environment(agent=AGENT_CLAUDE).get(ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR) == "1"
    )
    if args.write_usage:
        try:
            if not enabled:
                raise ValueError("savings accounting is disabled")
            document = json.loads(sys.stdin.buffer.read())
            if (
                not isinstance(document, dict)
                or document.get("version") != _USAGE_VERSION
                or not isinstance(document.get("entries"), list)
            ):
                raise ValueError("unsupported savings usage snapshot")
            from ucode.config_io import atomic_write_json

            atomic_write_json(args.usage, document)
        except Exception:  # noqa: BLE001 - keep the snapshot in the mod for a later write
            print(json.dumps({"savings": None}))
            return 1
    line = (
        usage_output(
            args.usage,
            price_cache=args.price_cache,
            baseline_session_start=args.baseline_session_start,
        )
        if enabled
        else json.dumps({"savings": None})
    )
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
