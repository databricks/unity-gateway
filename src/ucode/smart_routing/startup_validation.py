"""Startup Smart Router menu check; an unsupported menu turns routing off for the session.

A failed or timed-out check is silent and leaves routing on.
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


def _message(payload: object, reason: str | None) -> str | None:
    if reason or not isinstance(payload, dict) or payload.get("is_supported") is not False:
        return None
    parts = ["Smart Router does not support this model menu."]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("routing_unsupported_reason"), str):
        parts.append(metadata["routing_unsupported_reason"])
    for option in payload.get("ineligible_route_options", [])[:3]:
        if isinstance(option, dict) and isinstance(option.get("reason"), str):
            parts.append(f"{option.get('model', 'Model')}: {option['reason']}")
    available = payload.get("available_route_options", [])
    suggestions = [
        f"{option['model']} ({option['harness']})" if option.get("harness") else option["model"]
        for option in available[:5]
        if isinstance(option, dict) and isinstance(option.get("model"), str)
    ]
    if suggestions:
        parts.append("Available options: " + ", ".join(suggestions) + ".")
    parts.append("Smart routing is off for this session.")
    return " ".join(" ".join(parts).split())[:1000]


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
            message = _message(payload, reason)
        except Exception:
            message = None
        result: dict[str, object] = {"message": message}
        if message:
            with suppress(OSError, RuntimeError):
                atomic_write_json(session_controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
                result["routing_disabled"] = True
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), result)

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
