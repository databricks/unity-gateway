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
from ucode.constants import AGENT_CLAUDE, MODEL_PROVIDER_SERVICE_HEADER, SMART_ROUTING_ENV_KEYS
from ucode.databricks import _http_post_json, workspace_origin
from ucode.smart_routing import routing

# The Claude mod shows routing ON after 5 s without a result, so the check must finish sooner.
# urllib applies the timeout per socket operation, so a result past this deadline counts as a timeout.
VALIDATION_TIMEOUT_SECONDS = 4
# available_route_options name the router's canonical harness, not the requested one.
CLAUDE_AVAILABLE_HARNESSES = (AGENT_CLAUDE, "claude_code")


def result_path(session_controls: Path) -> Path:
    return session_controls.with_suffix(".validation.json")


def _models(options: object, *, harnesses: tuple[str, ...] | None = None) -> list[str]:
    return [
        option["model"]
        for option in (options if isinstance(options, list) else [])
        if isinstance(option, dict)
        and isinstance(option.get("model"), str)
        and (harnesses is None or option.get("harness") in harnesses)
    ]


def _unsupported_details(payload: dict[str, Any], validation_ms: int) -> dict[str, Any]:
    metadata = payload.get("metadata")
    reason = metadata.get("routing_unsupported_reason") if isinstance(metadata, dict) else None
    ineligible = payload.get("ineligible_route_options")
    return {
        "reason": reason if isinstance(reason, str) else None,
        "eligible": _models(payload.get("eligible_route_options")),
        "ineligible": [
            {
                "model": option["model"],
                "reason": option.get("reason") if isinstance(option.get("reason"), str) else None,
            }
            for option in (ineligible if isinstance(ineligible, list) else [])
            if isinstance(option, dict) and isinstance(option.get("model"), str)
        ],
        "available": _models(
            payload.get("available_route_options"), harnesses=CLAUDE_AVAILABLE_HARNESSES
        ),
        "validation_ms": validation_ms,
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
    body = {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "route_selector": {"router_name": router_name},
    }

    def check() -> None:
        started = time.monotonic()
        try:
            payload, reason = _http_post_json(
                workspace_origin(workspace) + routing.VALIDATION_PATH,
                token,
                body,
                timeout=VALIDATION_TIMEOUT_SECONDS,
                headers={MODEL_PROVIDER_SERVICE_HEADER: provider} if provider else None,
            )
        except Exception as exc:
            payload, reason = None, exc
        if not reason and time.monotonic() - started > VALIDATION_TIMEOUT_SECONDS:
            reason = "startup validation timed out"
        if reason:
            metrics.failure(
                metrics.STARTUP_VALIDATION_OPERATION,
                reason,
                continued_with_fallback=True,
                phase="startup",
            )
        unsupported = (
            not reason and isinstance(payload, dict) and payload.get("is_supported") is False
        )
        result: dict[str, Any] = {"routing_disabled": False}
        if unsupported:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result["routing_disabled"] = True
                result.update(
                    _unsupported_details(payload, round((time.monotonic() - started) * 1000))
                )
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
