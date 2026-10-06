"""Inference-payload metadata shared by smart-routed agent integrations."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ucode.smart_routing.routing import configured_router_name

SMART_ROUTER_RECIPE_FIELD = "smart_router_recipe_name"
UG_ROUTING_STATE_TAG = "ug-routing-state"


def smart_router_recipe_payload(
    enabled: bool, env: Mapping[str, str] | None = None
) -> dict[str, str | None]:
    """Return the recipe override for an opted-in session's next inference."""
    return {
        SMART_ROUTER_RECIPE_FIELD: configured_router_name(env) if enabled else None,
    }


def smart_router_recipe_marker(enabled: bool, env: Mapping[str, str] | None = None) -> str:
    """Encode the recipe override as an exact developer-message marker."""
    payload = json.dumps(
        smart_router_recipe_payload(enabled, env),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"<{UG_ROUTING_STATE_TAG}>{payload}</{UG_ROUTING_STATE_TAG}>"
