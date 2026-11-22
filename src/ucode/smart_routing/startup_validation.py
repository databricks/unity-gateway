"""Startup Smart Router model-list check; an unsupported model list turns routing off for the session.

Nothing is shown to the user. A failed or timed-out check leaves routing on.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from ucode import metrics
from ucode.config_io import atomic_write_json
from ucode.constants import (
    MODEL_PROVIDER_SERVICE_HEADER,
    ROUTER_HARNESS_CLAUDE_CODE,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.databricks import _http_post_json, workspace_origin
from ucode.smart_routing import routing

# The Claude mod shows routing ON after 5 s without a result, so the check must finish sooner.
# urllib applies the timeout per socket operation, so a result past this deadline counts as a timeout.
VALIDATION_TIMEOUT_SECONDS = 4
IS_SUPPORTED_FIELD = "is_supported"


def result_path(session_controls: Path) -> Path:
    return session_controls.with_suffix(".validation.json")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _route_options(
    options: object, *, harness: str | None = None, with_reason: bool = False
) -> list[dict[str, str | None]]:
    return [
        {
            "model": option["model"],
            "harness": _text(option.get("harness")),
            **({"reason": _text(option.get("reason"))} if with_reason else {}),
        }
        for option in (options if isinstance(options, list) else [])
        if isinstance(option, dict)
        and isinstance(option.get("model"), str)
        and (harness is None or option.get("harness") == harness)
    ]


def _unsupported_details(payload: dict[str, Any], router_name: str) -> dict[str, Any]:
    metadata = payload.get("metadata")
    return {
        "router_name": router_name,
        "routing_unsupported_reason": _text(
            metadata.get("routing_unsupported_reason") if isinstance(metadata, dict) else None
        ),
        "eligible_route_options": _route_options(payload.get("eligible_route_options")),
        "ineligible_route_options": _route_options(
            payload.get("ineligible_route_options"), with_reason=True
        ),
        "available_route_options": _route_options(
            payload.get("available_route_options"), harness=ROUTER_HARNESS_CLAUDE_CODE
        ),
    }


@metrics.timed(
    metrics.ROUTE_VALIDATION_DURATION_MS, reason_result=True, continued_with_fallback=True
)
def _validate_route_options(
    workspace: str, token: str, body: dict[str, Any], provider: str | None
) -> tuple[object, str | None]:
    started = time.monotonic()
    payload, reason = _http_post_json(
        workspace_origin(workspace) + routing.VALIDATION_PATH,
        token,
        body,
        timeout=VALIDATION_TIMEOUT_SECONDS,
        headers={MODEL_PROVIDER_SERVICE_HEADER: provider} if provider else None,
    )
    if not reason and time.monotonic() - started > VALIDATION_TIMEOUT_SECONDS:
        return None, "route validation timed out"
    return payload, reason


def validate(
    workspace: str,
    token: str,
    route_options: list[tuple[str, str]],
    *,
    router_name: str,
    provider: str | None = None,
) -> dict[str, Any] | None:
    """Return the validation result, or None when the check fails or misses its deadline."""
    body = {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "route_selector": {"router_name": router_name},
    }
    started = time.monotonic()
    try:
        payload, reason = _validate_route_options(workspace, token, body, provider)
    except Exception:
        return None
    if reason or not isinstance(payload, dict):
        return None
    if payload.get(IS_SUPPORTED_FIELD) is not False:
        return {IS_SUPPORTED_FIELD: True}
    return {
        IS_SUPPORTED_FIELD: False,
        **_unsupported_details(payload, router_name),
        "validation_ms": round((time.monotonic() - started) * 1000),
    }


def start(
    workspace: str,
    token: str,
    route_options: list[tuple[str, str]],
    *,
    router_name: str,
    session_controls: Path,
    provider: str | None = None,
) -> None:
    def check() -> None:
        validation = validate(
            workspace, token, route_options, router_name=router_name, provider=provider
        )
        result: dict[str, Any] = {"routing_disabled": False}
        if validation and validation[IS_SUPPORTED_FIELD] is False:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result["routing_disabled"] = True
                result.update({k: v for k, v in validation.items() if k != IS_SUPPORTED_FIELD})
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
