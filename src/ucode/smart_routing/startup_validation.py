"""Startup Smart Router menu check; an unsupported menu turns routing off for the session.

Nothing is shown to the user. A failed or timed-out check leaves routing on.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from pathlib import Path

from ucode import metrics
from ucode.config_io import atomic_write_json
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER, SMART_ROUTING_ENV_KEYS
from ucode.databricks import _http_post_json, workspace_origin
from ucode.smart_routing import routing

# The Claude mod shows routing ON after 5 s without a result, so the check must finish sooner.
# urllib applies the timeout per socket operation, so the whole check also has this deadline.
VALIDATION_TIMEOUT_SECONDS = 4


def result_path(session_controls: Path) -> Path:
    return session_controls.with_suffix(".validation.json")


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
        result = {"routing_disabled": False}
        if unsupported:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result["routing_disabled"] = True
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
