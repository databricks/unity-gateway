"""Best-effort startup milestones for an instrumented parent launcher."""

from __future__ import annotations

import functools
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable

LAUNCH_CONTEXT_ENV = "ISAAC_CLI_METRICS_CONTEXT"
UG_ENTRY_ENV = "ISAAC_UG_ENTRY_MONOTONIC_NS"
UG_HANDOFF_ENV = "ISAAC_UG_HANDOFF_MONOTONIC_NS"


def record_entry() -> None:
    if os.environ.get(LAUNCH_CONTEXT_ENV):
        os.environ[UG_ENTRY_ENV] = str(time.monotonic_ns())


def _context() -> dict | None:
    raw = os.environ.get(LAUNCH_CONTEXT_ENV, "")
    if not raw or len(raw) > 4096:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    port = value.get("event_port")
    if type(port) is not int or not 0 < port < 65536:
        return None
    for field in ("key", "launch_id"):
        item = value.get(field)
        if not isinstance(item, str) or not item or len(item) > 128:
            return None
    return value


def _send(event: str, **attributes: str | int) -> None:
    context = _context()
    if context is None:
        return
    try:
        payload = json.dumps(
            {
                "version": 1,
                "key": context["key"],
                "launch_id": context["launch_id"],
                "event": event,
                "monotonic_ns": time.monotonic_ns(),
                **attributes,
            },
            separators=(",", ":"),
        ).encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(payload, ("127.0.0.1", context["event_port"]))
    except (OSError, TypeError, ValueError, UnicodeError):
        pass


def record_dispatch() -> None:
    """Separate UG preparation from agent-specific launch preparation."""
    _send("ug_dispatch")


def record_handoff() -> None:
    if _context() is None:
        return
    os.environ[UG_HANDOFF_ENV] = str(time.monotonic_ns())
    _send("ug_handoff")


def metrics_trace_endpoint(harness: str, forward_url: str | None) -> str | None:
    """Retain an instrumented launcher's trace observer across UG configuration."""
    if _context() is None:
        return None
    raw = os.environ.get("ISAAC_CLI_METRICS_RECEIVER", "")
    if not raw or len(raw) > 4096:
        return None
    try:
        info = json.loads(raw)
        port = info.get("http_port")
        key = info.get("key")
        if (
            type(port) is not int
            or not 0 < port < 65536
            or not isinstance(key, str)
            or len(key) != 64
        ):
            return None
        url = f"http://127.0.0.1:{port}/{key}/register"
        payload = json.dumps(
            {"harness": harness, "signal": "traces", "forward_url": forward_url}
        ).encode()
        request = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.25) as response:
            result = json.loads(response.read(4096))
        route = result.get("route_id")
        if not isinstance(route, str) or len(route) != 32:
            return None
        return f"http://127.0.0.1:{port}/{key}/{route}"
    except (AttributeError, OSError, TypeError, ValueError, urllib.error.URLError):
        return None


def observe_claude_metrics(env: dict) -> None:
    if env.get("CLAUDE_CODE_ENABLE_TELEMETRY") != "1":
        return
    original = (
        env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        if env.get("OTEL_TRACES_EXPORTER") == "otlp"
        else None
    )
    endpoint = metrics_trace_endpoint("claude", original)
    if endpoint is not None:
        env.update(
            {
                "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "OTEL_TRACES_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
            }
        )


def observe_agent_launch[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        context = _context()
        if context is None:
            return function(*args, **kwargs)
        entry = os.environ.get(UG_ENTRY_ENV)
        if entry is not None and len(entry) <= 20 and entry.isascii() and entry.isdecimal():
            _send("ug_entry", entry_monotonic_ns=int(entry))
        else:
            _send("ug_entry", entry_monotonic_ns=time.monotonic_ns())
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            code = getattr(error, "exit_code", getattr(error, "code", None))
            if isinstance(error, KeyboardInterrupt) or code == 130:
                _send("startup_cancelled", component="ug")
            elif not isinstance(error, SystemExit) or code not in (None, 0):
                _send("startup_failed", component="ug", failure_kind="setup_error")
            raise

    return wrapped
