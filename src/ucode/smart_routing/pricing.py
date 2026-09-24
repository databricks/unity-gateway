"""Per-token model prices and the cost of coding-agent token usage.

The launcher caches prices at ``PRICE_CACHE_FILENAME`` for the savings statusline to read; this
module defines that cache and the arithmetic. The AI Gateway price source is not wired in yet, so
nothing writes the cache. Whatever supplies it must give, for each model:

- an id that ``model_key`` matches to the ``model`` Claude Code records for each response (the
  gateway's Messages response ``model``) and to the statusline's ``model.id``;
- USD per million tokens for input, output, cache read, 5-minute and 1-hour cache writes, plus any
  long-context tier (the prompt-token threshold and its rates);
- no entry, or no rate, where the model or a token class isn't priced; never zero.

Stdlib-only on purpose: the savings statusline imports this on every Claude Code refresh, and the
CLI's usual imports (Rich, Typer, the Databricks SDK, even ``urllib.request``) cost enough startup
for Claude Code to cancel the run.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

PRICE_CACHE_FILENAME = "model-prices.json"
_PRICE_CACHE_VERSION = 1
_MILLION = Decimal(1_000_000)
_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")
_ANTHROPIC_AIGW_PREFIX_RE = re.compile(r"^anthropic-aigw-[0-9a-f]{8}-")


def model_key(model: str) -> str:
    """Collapse the spellings of one model id that Claude Code and the gateway use.

    Mirrors ``routing.unwrap_anthropic_gateway_model`` + ``routing.normalize_model`` (and drops a
    ``[1m]`` context-window selector, which names a window rather than a price) without importing
    ``routing``, whose ``urllib.request`` import alone costs ~0.2s of statusline startup.
    """
    name = _CONTEXT_SUFFIX_RE.sub("", (model or "").strip().lower())
    name = _ANTHROPIC_AIGW_PREFIX_RE.sub("", name).rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens by token class; ``None`` where the gateway reports no rate."""

    input: Decimal | None = None
    output: Decimal | None = None
    cache_read: Decimal | None = None
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None
    # Rates for requests whose prompt exceeds the threshold (e.g. Sonnet 4.x above 200k tokens).
    long_context_threshold: int | None = None
    long_context: ModelPrice | None = None


_RATE_FIELDS = tuple(
    field.name for field in fields(ModelPrice) if not field.name.startswith("long_context")
)


class TokenUsage(NamedTuple):
    """One model response's billed tokens, split by the classes that are priced separately."""

    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0

    @classmethod
    def from_message_usage(cls, usage: Mapping[str, Any]) -> TokenUsage:
        """Read an Anthropic Messages ``usage`` object as Claude Code records it in transcripts.

        Cache writes are split by TTL because a 1-hour write bills at twice the input rate and a
        5-minute write at 1.25x; ug enables 1-hour caching. Writes the breakdown doesn't cover are
        billed as 5-minute writes, the API's default TTL.
        """
        creation = usage.get("cache_creation")
        creation = creation if isinstance(creation, Mapping) else {}
        write_1h = _count(creation.get("ephemeral_1h_input_tokens"))
        write_5m = _count(creation.get("ephemeral_5m_input_tokens"))
        write_5m += max(_count(usage.get("cache_creation_input_tokens")) - write_1h - write_5m, 0)
        return cls(
            input=_count(usage.get("input_tokens")),
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cache_read=_count(usage.get("cache_read_input_tokens")),
            output=_count(usage.get("output_tokens")),
        )

    @property
    def prompt_tokens(self) -> int:
        """Every input-side token, which is what long-context price thresholds measure."""
        return self.input + self.cache_write_5m + self.cache_write_1h + self.cache_read


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def token_cost(price: ModelPrice, tokens: TokenUsage) -> Decimal | None:
    """USD cost of one response, or None when a token class it used has no rate.

    Returning None rather than pricing a missing rate at zero keeps callers from showing a figure
    that silently undercounts.
    """
    if (
        price.long_context is not None
        and price.long_context_threshold is not None
        and tokens.prompt_tokens > price.long_context_threshold
    ):
        price = price.long_context
    total = Decimal(0)
    for count, rate in (
        (tokens.input, price.input),
        (tokens.cache_write_5m, price.cache_write_5m),
        (tokens.cache_write_1h, price.cache_write_1h),
        (tokens.cache_read, price.cache_read),
        (tokens.output, price.output),
    ):
        if count <= 0:
            continue
        if rate is None:
            return None
        total += Decimal(count) * rate
    return total / _MILLION


def _rate(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str) or not str(raw).strip():
        return None
    try:
        rate = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return rate if rate.is_finite() and rate >= 0 else None


def _dump_price(price: ModelPrice) -> dict[str, Any]:
    dumped: dict[str, Any] = {
        name: str(value) for name in _RATE_FIELDS if (value := getattr(price, name)) is not None
    }
    if price.long_context is not None and price.long_context_threshold is not None:
        dumped["long_context_threshold"] = price.long_context_threshold
        dumped["long_context"] = _dump_price(price.long_context)
    return dumped


def _load_price(raw: object) -> ModelPrice | None:
    if not isinstance(raw, Mapping):
        return None
    rates = {name: _rate(raw.get(name)) for name in _RATE_FIELDS}
    threshold = raw.get("long_context_threshold")
    long_context = _load_price(raw.get("long_context"))
    if isinstance(threshold, bool) or not isinstance(threshold, int) or long_context is None:
        threshold, long_context = None, None
    return ModelPrice(**rates, long_context_threshold=threshold, long_context=long_context)


def write_price_cache(
    path: Path, prices: Mapping[str, ModelPrice], *, now: float | None = None
) -> None:
    """Atomically replace the price cache so a concurrent statusline never reads a partial file.

    ``prices`` is keyed by any spelling of the model id; entries are stored under ``model_key``.
    """
    payload = {
        "version": _PRICE_CACHE_VERSION,
        "fetched_at": now if now is not None else time.time(),
        "models": {model_key(model): _dump_price(price) for model, price in prices.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_price_cache(path: Path) -> tuple[dict[str, ModelPrice], str] | None:
    """Load prices keyed by ``model_key``, plus a fingerprint that changes on every refresh."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _PRICE_CACHE_VERSION:
        return None
    models = payload.get("models")
    if not isinstance(models, dict):
        return None
    prices = {
        key: price
        for key, raw in models.items()
        if isinstance(key, str) and (price := _load_price(raw)) is not None
    }
    if not prices:
        return None
    return prices, str(payload.get("fetched_at"))
