"""Opt-in system.ai model pricing for Claude Code's managed settings.

``ug configure`` fetches per-million-token rates for the workspace's Databricks-hosted model
services from the v2 endpoint-rates API and writes them into the managed settings as a
``modelPricing.overrides`` block, so Claude Code prices sessions in USD instead of guessing at
list price. The whole feature stays off unless ``UG_MODEL_PRICING`` opts in: off, ug never
calls the rates API, writes no pricing state, and the settings render exactly as they did
before pricing existed.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from ucode.databricks import _http_get_json, get_databricks_token, workspace_origin
from ucode.env import env_bool
from ucode.ui import print_warning

# Opt-in gate for every pricing behavior: the rates fetch, the state cache, and the
# modelPricing block the managed settings carry.
MODEL_PRICING_ENV = "UG_MODEL_PRICING"

# `ug configure` caches the workspace's USD price rows here, keyed by model service FQN, so a
# plain launch writes the same modelPricing block without another rates round trip. The key
# keeps the `claude_` prefix because Claude Code is its only consumer today.
MODEL_PRICING_STATE_KEY = "claude_model_pricing"


def model_pricing_enabled() -> bool:
    return env_bool(MODEL_PRICING_ENV)


# Pay-per-token rates for Databricks-hosted (system.ai) model services: DBU per million tokens
# plus the workspace's estimated USD conversion. Served by the v2 endpoint-rates API, which
# lists only services visible to the caller and silently omits unknown names.
_ENDPOINT_RATES_API_PATH = "/api/ai-gateway/v2/endpoint-rates:batchGet"

# Wire `token_type` enum names to the short kinds the overrides builder reads.
_ENDPOINT_RATE_TOKEN_TYPES = {
    "TOKEN_TYPE_INPUT": "input",
    "TOKEN_TYPE_OUTPUT": "output",
    "TOKEN_TYPE_CACHE_READ": "cache_read",
    "TOKEN_TYPE_CACHE_CREATION": "cache_creation",
    "TOKEN_TYPE_CACHE_CREATION_1H": "cache_creation_1h",
}


def fetch_model_service_rates(
    workspace: str, token: str, model_services: list[str]
) -> tuple[dict[str, dict[str, dict[str, float]]], str | None]:
    """Batch-fetch per-million-token rates for system.ai model services.

    Returns ``(rates, reason)`` where ``rates`` maps each model service FQN to
    ``{unit: {token kind: cost per million tokens}}`` (unit is ``"DBU"`` or ``"USD"``).
    ``reason`` is non-None on failure; callers drop pricing rather than fail.
    """
    if not model_services:
        return {}, None
    query = urlencode({"databricks_hosted_model_services": model_services}, doseq=True)
    url = f"{workspace_origin(workspace)}{_ENDPOINT_RATES_API_PATH}?{query}"
    payload, reason = _http_get_json(url, token, timeout=30)
    if payload is None:
        return {}, reason
    if not isinstance(payload, dict):
        return {}, "endpoint-rates returned an unexpected response shape"
    rates: dict[str, dict[str, dict[str, float]]] = {}
    for entry in payload.get("model_service_rates") or []:
        if not isinstance(entry, dict):
            continue
        model_service = entry.get("model_service")
        if not isinstance(model_service, str) or not model_service:
            continue
        per_unit: dict[str, dict[str, float]] = {}
        for costs in entry.get("costs") or []:
            if not isinstance(costs, dict):
                continue
            unit = costs.get("unit")
            if not isinstance(unit, str) or not unit:
                continue
            token_costs: dict[str, float] = {}
            for token_cost in costs.get("token_costs") or []:
                if not isinstance(token_cost, dict):
                    continue
                kind = _ENDPOINT_RATE_TOKEN_TYPES.get(token_cost.get("token_type"))
                cost = token_cost.get("cost")
                if kind and isinstance(cost, int | float) and not isinstance(cost, bool):
                    token_costs[kind] = float(cost)
            if token_costs:
                per_unit[unit] = token_costs
        if per_unit:
            rates[model_service] = per_unit
    return rates, None


# Gateway discovery aliases embed the canonical FQN after their prefix, e.g.
# ``anthropic-aigw-<hash>-system.ai.glm-5-3``.
_ALIASED_FQN_RE = re.compile(r"(system\.ai\.[A-Za-z0-9_.-]+)$")


def pricing_fqn(model: str) -> str | None:
    """The rates API's system.ai FQN a picker id names, or ``None`` when it names none.

    Drops ug's own ``[1m]`` hint and unwraps gateway discovery aliases (which carry a hash
    prefix Claude Code sessions send verbatim). Plain non-system.ai ids name nothing the
    rates API can serve.
    """
    model = model.removesuffix("[1m]")
    if model.startswith("system.ai."):
        return model
    match = _ALIASED_FQN_RE.search(model)
    return match.group(1) if match else None


def usd_pricing_overrides(
    rates: dict[str, dict[str, dict[str, float]]],
) -> dict[str, dict[str, float]]:
    """Claude Code ``modelPricing.overrides`` rows (USD per million tokens) from served rates.

    A row needs the USD input, output and cache-read rates: Claude Code's schema requires all
    four fields, so a model served only in DBU (or missing one of them) is skipped rather than
    priced partly or with DBU numbers written as dollars. ``cacheWrite`` is Claude Code's single
    cache-write rate covering both cache lifetimes, so take the 1-hour write rate first (ug
    enables 1-hour prompt caching), then the 5-minute one, then the input rate — a cache write is
    never cheaper than processing the same token as fresh input.
    """
    overrides: dict[str, dict[str, float]] = {}
    for fqn, per_unit in rates.items():
        usd = per_unit.get("USD") or {}
        if any(usd.get(kind) is None for kind in ("input", "output", "cache_read")):
            continue
        cache_write = next(
            (
                rate
                for kind in ("cache_creation_1h", "cache_creation", "input")
                if (rate := usd.get(kind)) is not None
            ),
        )
        overrides[fqn] = {
            "input": round(usd["input"], 4),
            "output": round(usd["output"], 4),
            "cacheRead": round(usd["cache_read"], 4),
            "cacheWrite": round(cache_write, 4),
        }
    return overrides


def cached_model_pricing(state: dict) -> dict[str, dict[str, float]] | None:
    """The price rows ``ug configure`` cached, for a launch that must not fetch them itself.

    ``None`` while the feature is off, so a stale cache from an earlier priced run never
    reaches the managed settings.
    """
    if not model_pricing_enabled():
        return None
    return state.get(MODEL_PRICING_STATE_KEY) or None


def refresh_model_pricing(state: dict, models: list[str]) -> dict[str, dict[str, float]]:
    """Fetch system.ai rates for ``models``, cache their USD price rows, and return them.

    ``ug configure`` calls this so the managed settings carry a ``modelPricing`` block; a plain
    launch renders from the cache through ``cached_model_pricing``. A failed fetch leaves the
    settings unpriced rather than failing configuration, and clears the cache so a launch's
    file still matches. ``UG_MODEL_PRICING`` off skips the fetch and every cache write.
    """
    if not model_pricing_enabled():
        return {}
    fqns = sorted({fqn for fqn in map(pricing_fqn, models) if fqn is not None})
    if not fqns:
        return {}
    workspace = state["workspace"]
    try:
        token = get_databricks_token(workspace, state.get("profile"))
        rates, reason = fetch_model_service_rates(workspace, token, fqns)
    except (OSError, RuntimeError) as exc:
        rates, reason = {}, str(exc)
    if reason is not None:
        # Clear the cache too: a launch renders whatever is cached, so keeping stale prices
        # here would make its file differ from this unpriced write.
        state[MODEL_PRICING_STATE_KEY] = {}
        print_warning(f"Model prices are unavailable for the model picker: {reason}")
        return {}
    rows = usd_pricing_overrides(rates)
    # Key every row by its canonical FQN and by each picker id that names it: a session on an
    # aliased model sends the alias verbatim, so Claude Code's exact-key lookup needs it too.
    pricing: dict[str, dict[str, float]] = {}
    for model in models:
        fqn = pricing_fqn(model)
        if fqn is None or fqn not in rows:
            continue
        pricing.setdefault(fqn, rows[fqn])
        pricing.setdefault(model.removesuffix("[1m]"), rows[fqn])
    state[MODEL_PRICING_STATE_KEY] = pricing
    return pricing
