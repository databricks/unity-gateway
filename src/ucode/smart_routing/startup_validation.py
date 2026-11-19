"""Startup Smart Router menu check; an unsupported menu turns routing off for the session.

Nothing is shown to the user. A failed or timed-out check leaves routing on.
"""

from __future__ import annotations

import threading
from contextlib import suppress
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.databricks import _http_post_json, workspace_origin

_VALIDATION_PATH = "/ai-gateway/routing/v1/routes:validate"
# The Claude mod shows routing ON after 5 s without a result, so the check must finish sooner.
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
) -> None:
    body = {
        "route_options": [{"model": model, "harness": harness} for model, harness in route_options],
        "route_selector": {"router_name": router_name},
    }

    def check() -> None:
        try:
            payload, reason = _http_post_json(
                workspace_origin(workspace) + _VALIDATION_PATH,
                token,
                body,
                timeout=VALIDATION_TIMEOUT_SECONDS,
            )
            unsupported = (
                not reason and isinstance(payload, dict) and payload.get("is_supported") is False
            )
        except Exception:
            unsupported = False
        result = {"routing_disabled": False}
        if unsupported:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result["routing_disabled"] = True
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
