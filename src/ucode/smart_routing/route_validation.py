"""Smart Router model-list check at startup and on `/smart-router on`, shared by both harnesses.

At startup, an unsupported model list or a failed check turns routing off for the session. HTTP 404
is the flag-off answer from the endpoint, so it leaves routing on and records nothing. On
`/smart-router on`, the caller reads the verdict before turning routing on.
"""

from __future__ import annotations

import contextvars
import re
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from ucode import metrics
from ucode.config_io import atomic_write_json
from ucode.constants import (
    ISAAC_LAUNCH_MODE_ENV_VAR,
    ISAAC_OMNI_LAUNCH_MODE,
    MODEL_PROVIDER_SERVICE_HEADER,
    ROUTER_HARNESS_CLAUDE_CODE,
    SMART_ROUTING_ENV_KEYS,
)
from ucode.databricks import _http_post_json, workspace_origin
from ucode.smart_routing import routing
from ucode.smart_routing.session_env import validation_result_path
from ucode.workspace import WorkspaceSelectionReason

# The Claude mod shows routing ON after 11 s without a result, so the check must publish sooner.
VALIDATION_TIMEOUT_SECONDS = 10
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


def is_route_validation_enabled(source: Mapping[str, str]) -> bool:
    """Omnigent launches skip route validation until Omnigent shows its result."""
    return source.get(ISAAC_LAUNCH_MODE_ENV_VAR) != ISAAC_OMNI_LAUNCH_MODE


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


def _unsupported_details(
    payload: dict[str, Any], router_name: str, available_harness: str
) -> dict[str, Any]:
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
            payload.get("available_route_options"), harness=available_harness
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


def _request_body(route_options: list[tuple[str, str]], router_name: str) -> dict[str, Any]:
    return {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "route_selector": {"router_name": router_name},
    }


def _verdict(
    payload: object,
    reason: str | None,
    router_name: str,
    started: float,
    available_harness: str,
) -> dict[str, Any]:
    if reason and reason.startswith(NOT_FOUND_REASON_PREFIX):
        return {"routing_disabled": False}
    failure = _failure_reason(payload, reason)
    if failure is None and not _is_unsupported(payload):
        return {"routing_disabled": False}
    details = (
        _failure_details(failure, router_name)
        if failure is not None
        else _unsupported_details(cast(dict, payload), router_name, available_harness)
    )
    return {
        "routing_disabled": True,
        **details,
        "validation_ms": round((time.monotonic() - started) * 1000),
    }


def check_failed(router_name: str, started: float) -> dict[str, Any]:
    """Return the verdict for a check that could not run."""
    return {
        "routing_disabled": True,
        **_failure_details(CHECK_FAILED_REASON, router_name),
        "validation_ms": round((time.monotonic() - started) * 1000),
    }


def validate(
    workspace: str,
    token: str,
    route_options: list[tuple[str, str]],
    *,
    router_name: str,
    provider: str | None = None,
    available_harness: str = ROUTER_HARNESS_CLAUDE_CODE,
) -> dict[str, Any]:
    """Return the verdict; an unsupported list or a failed check disables routing."""
    started = time.monotonic()
    try:
        payload, reason = _check_route_options(
            workspace, token, _request_body(route_options, router_name), provider
        )
    except Exception:
        payload, reason = None, CHECK_FAILED_REASON
    return _verdict(payload, reason, router_name, started, available_harness)


def start(
    workspace: str,
    token: str,
    route_options: list[tuple[str, str]],
    *,
    router_name: str,
    session_controls: Path,
    provider: str | None = None,
    available_harness: str = ROUTER_HARNESS_CLAUDE_CODE,
) -> None:
    body = _request_body(route_options, router_name)

    def check() -> None:
        started = time.monotonic()
        payload, reason = _check_route_options(workspace, token, body, provider)
        result = _verdict(payload, reason, router_name, started, available_harness)
        if result["routing_disabled"]:
            try:
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
            except (OSError, RuntimeError):
                result = {"routing_disabled": False}
        _publish(validation_result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
