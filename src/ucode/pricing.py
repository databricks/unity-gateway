"""Opt-in system.ai model pricing for ug's model pickers.

``ug configure`` fetches per-million-token rates for the workspace's Databricks-hosted model
services from the v2 endpoint-rates API and renders one price line per model row in Claude
Code's managed model picker. The whole feature stays off unless ``UG_MODEL_PRICING`` opts in:
off, ug never calls the rates API, writes no pricing state, and pickers render exactly as they
did before pricing existed.
"""

from __future__ import annotations

from urllib.parse import urlencode

from ucode.databricks import _http_get_json, get_databricks_token, workspace_origin
from ucode.env import env_bool
from ucode.ui import print_warning

# Opt-in gate for every pricing behavior: the rates fetch, the state cache, and the price lines
# a picker renders.
MODEL_PRICING_ENV = "UG_MODEL_PRICING"

# `ug configure` caches the workspace's model prices here, keyed by model service FQN, so a
# plain launch renders the same picker rows without another rates round trip. The key keeps
# the `claude_` prefix because the Claude model picker is its only consumer today.
MODEL_PRICING_STATE_KEY = "claude_model_pricing"


def model_pricing_enabled() -> bool:
    return env_bool(MODEL_PRICING_ENV)


# Pay-per-token rates for Databricks-hosted (system.ai) model services: DBU per million tokens
# plus the workspace's estimated USD conversion. Served by the v2 endpoint-rates API, which
# lists only services visible to the caller and silently omits unknown names.
_ENDPOINT_RATES_API_PATH = "/api/ai-gateway/v2/endpoint-rates:batchGet"

# Wire `token_type` enum names to the short kinds the picker renders.
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


def pricing_fqn(model: str) -> str:
    """The rates API's model service FQN for a picker id: drop ug's own ``[1m]`` hint."""
    return model.removesuffix("[1m]")


def pricing_for(model_pricing: dict[str, str] | None, model: str) -> str | None:
    """The cached pricing line for a picker model id, matched by FQN."""
    return (model_pricing or {}).get(pricing_fqn(model))


def pricing_text(unit_rates: dict[str, dict[str, float]]) -> str | None:
    """One picker line of per-million-token prices from a model's rates grouped by unit.

    USD is the reader-facing unit, but it is a workspace-wide list-price estimate of the DBU
    rate; the DBU rate is the authoritative fallback when no USD estimate is served.
    """
    for unit, price_fmt in (("USD", "${:.2f}"), ("DBU", "{:.2f} DBU")):
        rates = unit_rates.get(unit) or {}
        parts = [
            f"{price_fmt.format(rates[kind])} {kind.replace('_', ' ')}"
            for kind in ("input", "output", "cache_read")
            if rates.get(kind) is not None
        ]
        if parts:
            suffix = " (est.)" if unit == "USD" else ""
            return f"{' / '.join(parts)} per 1M tokens{suffix}"
    return None


def cached_model_pricing(state: dict) -> dict[str, str] | None:
    """The prices ``ug configure`` cached, for a launch that must not fetch them itself.

    ``None`` while the feature is off, so a stale cache from an earlier priced run never
    reaches a picker row.
    """
    if not model_pricing_enabled():
        return None
    return state.get(MODEL_PRICING_STATE_KEY) or None


def refresh_model_pricing(state: dict, models: list[str]) -> dict[str, str]:
    """Fetch system.ai rates for ``models``, cache their pricing lines, and return them.

    ``ug configure`` calls this so the managed picker carries prices; a plain launch renders
    from the cache through ``cached_model_pricing``. A failed fetch leaves the picker unpriced
    rather than failing configuration, and clears the cache so a launch's file still matches.
    ``UG_MODEL_PRICING`` off skips the fetch and every cache write.
    """
    if not model_pricing_enabled():
        return {}
    fqns = sorted({pricing_fqn(model) for model in models if model.startswith("system.ai.")})
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
    pricing = {fqn: text for fqn, unit_rates in rates.items() if (text := pricing_text(unit_rates))}
    state[MODEL_PRICING_STATE_KEY] = pricing
    return pricing
