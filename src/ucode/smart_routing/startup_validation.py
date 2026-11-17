"""One advisory compatibility check for a resolved smart-routing launch."""

from __future__ import annotations

import threading
from contextlib import suppress
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.databricks import _http_post_json, workspace_origin

_VALIDATION_PATH = "/ai-gateway/routing/v1/routes:validate"
_UNAVAILABLE = "Smart Router compatibility check unavailable. Routing remains enabled."


def result_path(session_controls: Path) -> Path:
    return session_controls.with_suffix(".validation.json")


def _message(payload: object, reason: str | None) -> str | None:
    if reason or not isinstance(payload, dict) or type(payload.get("is_supported")) is not bool:
        return _UNAVAILABLE
    if payload["is_supported"]:
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
    parts.append("Routing remains enabled.")
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
                workspace_origin(workspace) + _VALIDATION_PATH, token, body, timeout=10
            )
            message = _message(payload, reason)
        except Exception:
            message = _UNAVAILABLE
        with suppress(OSError, RuntimeError):
            atomic_write_json(result_path(session_controls), {"message": message})

    with suppress(Exception):
        threading.Thread(target=check, daemon=True).start()
