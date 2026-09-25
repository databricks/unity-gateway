"""Per-token model prices and the cost of coding-agent token usage.

The launcher fetches rates from the AI Gateway's endpoint-rates API (``prices_from_endpoint_rates``)
and caches them per workspace (``price_cache_path``) for the savings statusline to read; this module
defines that cache and the arithmetic. A token class with no rate makes a response unpriceable, and
the statusline hides the estimate rather than undercount it. The API returns input and output rates
today, so sessions that use prompt caching stay hidden until it also returns cache rates: fixed
multipliers don't hold across models (Opus 5.5 cache reads bill at 0.05x input, Opus 4.8's at 0.1x).

Stdlib-only on purpose: the savings statusline imports this on every Claude Code refresh, and the
CLI's usual imports (Rich, Typer, the Databricks SDK, even ``urllib.request``) cost enough startup
for Claude Code to cancel the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

_PRICE_CACHE_VERSION = 1
_MILLION = Decimal(1_000_000)
_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")
_ANTHROPIC_AIGW_PREFIX_RE = re.compile(r"^anthropic-aigw-[0-9a-f]{8}-")
# The gateway reports some served models by their provider id, e.g. Bedrock's
# `anthropic.claude-haiku-4-5-20251001-v1:0` for a request to `system.ai.claude-haiku-4-5`.
_PROVIDER_PREFIX_RE = re.compile(r"^(?:(?:us|eu|apac|au|jp|global)\.)?anthropic\.")
_PROVIDER_SUFFIX_RE = re.compile(r"(?:-20\d{6})?(?:-v\d+(?::\d+)?)?$")


def model_key(model: str) -> str:
    """Collapse the spellings of one model id that Claude Code and the gateway use.

    Mirrors ``routing.unwrap_anthropic_gateway_model`` + ``routing.normalize_model`` without
    importing ``routing``, whose ``urllib.request`` import alone costs ~0.2s of statusline startup.
    Also drops a ``[1m]`` context-window selector (a window, not a price) and the provider prefix
    and date/version suffix of a served id, so it keys the same as the requested model.
    """
    name = _CONTEXT_SUFFIX_RE.sub("", (model or "").strip().lower())
    name = _ANTHROPIC_AIGW_PREFIX_RE.sub("", name).rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    name = _PROVIDER_PREFIX_RE.sub("", name).split("@", 1)[0]
    return _PROVIDER_SUFFIX_RE.sub("", name)


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


def prices_from_endpoint_rates(rates: Iterable[object]) -> dict[str, ModelPrice]:
    """Map endpoint-rates ``EndpointRate`` entries to prices keyed by their model service.

    Uses ``cost_by_dollars``, which the API omits when the org has no DBU-to-dollar conversion
    configured; those models are left unpriced rather than shown in DBUs.
    """
    prices: dict[str, ModelPrice] = {}
    for rate in rates:
        if not isinstance(rate, Mapping):
            continue
        service = rate.get("model_service")
        dollars = rate.get("cost_by_dollars")
        if not isinstance(service, str) or not service or not isinstance(dollars, Mapping):
            continue
        # `TokenCostPerMillion` fields. The cache ones follow the gateway's existing
        # `ExternalModelPricing` names and are read once the endpoint-rates API returns them.
        price = ModelPrice(
            input=_rate(dollars.get("input_per_million_tokens")),
            output=_rate(dollars.get("output_per_million_tokens")),
            cache_read=_rate(dollars.get("cache_read_per_million_tokens")),
            cache_write_5m=_rate(dollars.get("cache_write_per_million_tokens")),
            cache_write_1h=_rate(dollars.get("cache_write_1hr_per_million_tokens")),
        )
        if price.input is not None or price.output is not None:
            prices[service] = price
    return prices


def price_cache_path(app_dir: Path, workspace: str) -> Path:
    """The workspace's price cache; dollar rates depend on its org's DBU conversion."""
    host = workspace.strip().lower().removeprefix("https://").rstrip("/")
    digest = hashlib.sha256(host.encode("utf-8")).hexdigest()[:16]
    return app_dir / f"model-prices-{digest}.json"


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
