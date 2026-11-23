"""Startup Smart Router model-list check.

An unsupported model list, or a failed check, turns routing off for the session. HTTP 404 is the
flag-off answer from the endpoint, so it leaves routing on and records nothing.
"""

from __future__ import annotations

import contextvars
import re
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
from ucode.workspace import WorkspaceSelectionReason

# The Claude mod shows routing ON after 5 s without a result, so the check must publish sooner.
VALIDATION_TIMEOUT_SECONDS = 4
NOT_FOUND_REASON_PREFIX = "HTTP 404"
HTTP_STATUS_PATTERN = re.compile(r"HTTP (\d{3})\b")
ROUTE_VALIDATION_TIMED_OUT = "route validation timed out"
TIMED_OUT_MARKER = "timed out"
NETWORK_ERROR_PREFIX = "network error:"
CHECK_FAILED_REASON = "Smart Router check failed unexpectedly"
TIMED_OUT_FAILURE_REASON = "Smart Router didn't respond in time"
HTTP_ERROR_FAILURE_REASON = "Smart Router returned an error (HTTP {status})"
UNREACHABLE_FAILURE_REASON = "Smart Router couldn't be reached"
UNEXPECTED_RESPONSE_FAILURE_REASON = "Smart Router returned an unexpected response"


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


def _is_unsupported(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("is_supported") is False


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


def _failure_details(failure: str, router_name: str) -> dict[str, Any]:
    return {
        "router_name": router_name,
        "validation_failed": True,
        "validation_failure_reason": failure,
        "eligible_route_options": [],
        "ineligible_route_options": [],
        "available_route_options": [],
    }


def _failure_reason(payload: object, reason: str | None) -> str | None:
    if reason is None:
        supported = payload.get("is_supported") if isinstance(payload, dict) else None
        return None if isinstance(supported, bool) else UNEXPECTED_RESPONSE_FAILURE_REASON
    if isinstance(reason, WorkspaceSelectionReason):
        return str(reason)
    if status := HTTP_STATUS_PATTERN.match(reason):
        return HTTP_ERROR_FAILURE_REASON.format(status=status.group(1))
    if reason == CHECK_FAILED_REASON:
        return CHECK_FAILED_REASON
    if TIMED_OUT_MARKER in reason:
        return TIMED_OUT_FAILURE_REASON
    if reason.startswith(NETWORK_ERROR_PREFIX):
        return UNREACHABLE_FAILURE_REASON
    return UNEXPECTED_RESPONSE_FAILURE_REASON


def _validate_route_options(
    workspace: str,
    token: str,
    body: dict[str, Any],
    provider: str | None,
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
        return None, ROUTE_VALIDATION_TIMED_OUT
    return payload, reason


def _publish(path: Path, result: dict[str, Any]) -> None:
    with suppress(OSError, RuntimeError):
        atomic_write_json(path, result)


def _probe(
    workspace: str,
    token: str,
    body: dict[str, Any],
    provider: str | None,
    outcome: dict[str, Any],
) -> None:
    try:
        outcome["response"] = _validate_route_options(workspace, token, body, provider)
    except Exception:
        outcome["response"] = (None, CHECK_FAILED_REASON)


@metrics.timed(
    metrics.ROUTE_VALIDATION_DURATION_MS, reason_result=True, continued_with_fallback=True
)
def _check_route_options(
    workspace: str,
    token: str,
    body: dict[str, Any],
    provider: str | None,
) -> tuple[object, str | None]:
    outcome: dict[str, Any] = {}
    # The probe shares this measurement, so its HTTP status labels it; a late probe only fills `outcome`.
    probe = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_probe, workspace, token, body, provider, outcome),
        daemon=True,
    )
    probe.start()
    probe.join(VALIDATION_TIMEOUT_SECONDS)
    return outcome.get("response", (None, ROUTE_VALIDATION_TIMED_OUT))


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
        payload, reason = _check_route_options(workspace, token, body, provider)
        if reason and reason.startswith(NOT_FOUND_REASON_PREFIX):
            _publish(result_path(session_controls), {"routing_disabled": False})
            return
        failure = _failure_reason(payload, reason)
        unsupported = _is_unsupported(payload)
        result: dict[str, Any] = {"routing_disabled": False}
        if failure is not None or unsupported:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result = (
                    {"routing_disabled": True, **_failure_details(failure, router_name)}
                    if failure is not None
                    else {"routing_disabled": True, **_unsupported_details(payload, router_name)}
                )
                result["validation_ms"] = round((time.monotonic() - started) * 1000)
        _publish(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
