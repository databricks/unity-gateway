"""Price the Claude mod's token sums into a smart-routing savings estimate.

Each routed child's tokens are compared with the main model selected when it ran.
Unrouted children add their actual cost to both totals, without a routing saving.
First-prompt routing uses the pre-routing main model for main and routed-child work
until a manual model switch. Unknown usage or child attribution hides the estimate.

The mod persists cumulative usage after each turn. Keep imports lightweight because
it runs this module in a fresh process to price each snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ucode.constants import AGENT_CLAUDE, ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR
from ucode.smart_routing.pricing import (
    ModelPrice,
    TokenUsage,
    model_key,
    read_price_cache_with_expiry,
    token_cost,
)

MODULE = "ucode.smart_routing.savings"
USAGE_FILENAME = "savings-usage.json"
_USAGE_VERSION = 1
_CENT = Decimal("0.01")


def usage_directory(app_dir: Path, workspace: str) -> Path:
    """Durable native-session snapshots for one fully selected workspace."""
    key = workspace.strip().lower().removeprefix("https://").rstrip("/")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return app_dir / "claude-savings" / digest


def _valid_token_usage(usage: object) -> bool:
    if not isinstance(usage, dict):
        return False

    def counts(source: dict, keys: tuple[str, ...]) -> bool:
        return all(
            key not in source or (type(source[key]) is int and source[key] >= 0) for key in keys
        )

    if not counts(
        usage,
        (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ),
    ):
        return False
    if "cache_creation" not in usage:
        return True
    split = usage["cache_creation"]
    return isinstance(split, dict) and counts(
        split, ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
    )


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
) -> tuple[Decimal, Decimal, float] | None:
    """The ``(saved, baseline, expires_at)`` estimate, or None with nothing to claim.

    Dollar totals expire at the earliest Unix expiry time of the rates they use.

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
    if document.get("incomplete_usage") is True:
        return None
    fixed_baseline = None
    if baseline_session_start:
        start = document.get("start_model")
        if not isinstance(start, str) or not start:
            return None
        fixed_baseline = start
    cached = read_price_cache_with_expiry(price_cache)
    if cached is None:
        return None
    prices = {key: price for key, (price, _) in cached.items()}
    expires_at = float("inf")

    actual_total = baseline_total = Decimal(0)
    rerouted = False
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        usage, served = entry.get("usage"), entry.get("served")
        agent = entry.get("agent")
        if not isinstance(agent, str) or not agent:
            return None
        if not _valid_token_usage(usage) or not isinstance(served, str) or not served:
            return None
        # Unsplit cache writes use the one-hour TTL configured for UG's Claude launches.
        tokens = TokenUsage.from_message_usage(usage, uncovered_writes_1h=True)
        if not any(tokens):
            continue
        routed = entry.get("routed")
        if agent != "main" and not isinstance(routed, bool):
            return None
        baseline = entry.get("baseline")
        if agent != "main" and not routed:
            baseline = served
        elif fixed_baseline is not None and entry.get("before_user_switch") is True:
            baseline = fixed_baseline
        if not isinstance(baseline, str) or not baseline:
            return None
        served_key, baseline_key = model_key(served), model_key(baseline)
        costs = _response_costs(prices, served_key, baseline_key, tokens)
        if costs is None:
            return None
        actual_total += costs[0]
        baseline_total += costs[1]
        expires_at = min(expires_at, cached[served_key][1], cached[baseline_key][1])
        rerouted = rerouted or served_key != baseline_key
    if not rerouted or baseline_total <= 0:
        return None
    return baseline_total - actual_total, baseline_total, expires_at


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
    output: dict[str, object] = {"savings": None}
    try:
        computed = usage_savings(
            usage_path, price_cache=price_cache, baseline_session_start=baseline_session_start
        )
        if computed is not None:
            saved, baseline, expires_at = computed
            valid_for_ms = int((expires_at - time.time()) * 1000)
            if valid_for_ms > 0:
                output = {
                    "savings": savings_segment(saved, baseline),
                    "valid_for_ms": valid_for_ms,
                }
    except Exception:  # noqa: BLE001
        pass
    return json.dumps(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=MODULE, description="Price the Claude mod's token sums as a JSON line."
    )
    parser.add_argument("--usage", type=Path, required=True)
    parser.add_argument(
        "--write-usage", action="store_true", help="Save the JSON snapshot on stdin."
    )
    parser.add_argument(
        "--no-price", action="store_true", help="Skip the optional savings estimate."
    )
    parser.add_argument("--price-cache", type=Path, required=True)
    parser.add_argument("--baseline-session-start", action="store_true")
    args = parser.parse_args(argv)
    if args.write_usage:
        try:
            document = json.loads(sys.stdin.buffer.read())
            if (
                not isinstance(document, dict)
                or document.get("version") != _USAGE_VERSION
                or not isinstance(document.get("entries"), list)
                or any(
                    not isinstance(entry, dict) or not _valid_token_usage(entry.get("usage"))
                    for entry in document["entries"]
                )
            ):
                raise ValueError("unsupported savings usage snapshot")
            from ucode.config_io import atomic_write_json

            atomic_write_json(args.usage, document)
        except Exception:  # noqa: BLE001 - keep the snapshot in the mod for a later write
            print(json.dumps({"savings": None}))
            return 1
    if args.no_price:
        print(json.dumps({"savings": None}))
        return 0

    from ucode.smart_routing.session_env import effective_environment

    enabled = (
        effective_environment(agent=AGENT_CLAUDE).get(ENABLE_SMART_ROUTING_SAVINGS_ENV_VAR) == "1"
    )
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
