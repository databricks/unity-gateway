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
from ucode.smart_routing.session_env import ROUTING_DISABLED_KEY, validation_result_path

# The Claude mod shows routing ON after 5 s without a result, so the check must finish sooner.
# urllib applies the timeout per socket operation, so a result past this deadline counts as a timeout.
VALIDATION_TIMEOUT_SECONDS = 4


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


def start(
    workspace: str,
    token: str,
    route_options: list[tuple[str, str]],
    *,
    router_name: str,
    session_controls: Path,
    provider: str | None = None,
) -> None:
    body = {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "route_selector": {"router_name": router_name},
    }

    def check() -> None:
        started = time.monotonic()
        try:
            payload, reason = _validate_route_options(workspace, token, body, provider)
        except Exception as exc:
            payload, reason = None, str(exc)
        unsupported = (
            not reason and isinstance(payload, dict) and payload.get("is_supported") is False
        )
        result: dict[str, Any] = {ROUTING_DISABLED_KEY: False}
        if unsupported:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result[ROUTING_DISABLED_KEY] = True
                result.update(_unsupported_details(payload, router_name))
                result["validation_ms"] = round((time.monotonic() - started) * 1000)
        with suppress(OSError, RuntimeError):
            atomic_write_json(validation_result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
