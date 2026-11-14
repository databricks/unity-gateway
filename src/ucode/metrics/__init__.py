"""Bounded performance observations; OTel aggregation and uploads run out of process."""

from __future__ import annotations

import inspect
import json
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

UG_STARTUP_DURATION_MS = "ug_startup_duration_ms"
CLI_IMPORT_DURATION_MS = "cli_import_duration_ms"
DEPENDENCY_SETUP_DURATION_MS = "dependency_setup_duration_ms"
DEPENDENCY_INSTALL_OPERATION = "dependency_install"
DEPENDENCY_INSTALL_DURATION_MS = f"{DEPENDENCY_INSTALL_OPERATION}_duration_ms"
AGENT_CONFIGURATION_DURATION_MS = "agent_configuration_duration_ms"
SKILLS_REFRESH_OPERATION = "skills_refresh"
SKILLS_REFRESH_DURATION_MS = f"{SKILLS_REFRESH_OPERATION}_duration_ms"
COMMAND_OPERATION = "command"
UG_CLI_ERROR = "ug_cli_error"

MAX_OBSERVATIONS = 1000
MAX_HANDOFF_BYTES = 2 * 1024 * 1024
EXPORT_INTERVAL_SECONDS = 60
DURATION_METRICS = frozenset(
    {
        UG_STARTUP_DURATION_MS,
        CLI_IMPORT_DURATION_MS,
        DEPENDENCY_SETUP_DURATION_MS,
        DEPENDENCY_INSTALL_DURATION_MS,
        "databricks_token_fetch_duration_ms",
        "databricks_login_duration_ms",
        "coding_agent_config_fetch_duration_ms",
        "model_service_access_check_duration_ms",
        "model_service_discovery_duration_ms",
        AGENT_CONFIGURATION_DURATION_MS,
        SKILLS_REFRESH_DURATION_MS,
        "routing_decision_duration_ms",
        "ug_usage_duration_ms",
        "http_request_duration_ms",
        "proxy_upstream_headers_duration_ms",
        "proxy_first_byte_duration_ms",
        "proxy_request_duration_ms",
    }
)
COUNTER_METRICS = frozenset({"http_retry_count", "proxy_auth_retry_count", UG_CLI_ERROR})
METRICS = DURATION_METRICS | COUNTER_METRICS | {"proxy_response_bytes"}
STRING_LABELS = frozenset(
    {
        "command",
        "agent",
        "dependency",
        "action",
        "token_source",
        "login_method",
        "config_scope",
        "routing_trigger",
        "api_operation",
        "http_method",
        "proxy_mode",
        "phase",
        "operation",
        "error_code",
    }
)
BOOL_LABELS = frozenset(
    {
        "preflight_skipped",
        "first_time_setup",
        "interactive_login",
        "cli_version_check_skipped",
        "interactive_wait",
        "in_process_cache_hit",
        "cache_hit",
        "deadline_reached",
        "show_all",
        "client_disconnected",
        "continued_with_fallback",
    }
)
INT_LABELS = frozenset({"http_status_code", "attempt", "process_exit_code"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.+\-]{1,128}\Z")
_scope: ContextVar[dict[str, Any] | None] = ContextVar("ug_metric_scope", default=None)
_runtime: Recorder | None = None
_AGENT_SESSION_COMMANDS = frozenset(
    {
        "auth-token",
        "otel-proxy",
        "otel-headers",
        "mcp-proxy",
        "claude-router-hook",
        "codex-router-hook",
        "cowork-hook",
        "claude-desktop-auth",
        "smart-router-describe",
        "smart-router-logs",
    }
)


def _best_effort(fn: Callable[..., None]) -> Callable[..., None]:
    # Use only for telemetry operations, never for the command being measured.
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with suppress(Exception):
            fn(*args, **kwargs)

    return wrapped


def _labels(values: dict[str, Any]) -> dict[str, str | bool | int]:
    result = {}
    for key, value in values.items():
        if key in STRING_LABELS and isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            result[key] = value
        elif key in BOOL_LABELS and type(value) is bool:
            result[key] = value
        elif key in INT_LABELS and type(value) is int and -(2**31) <= value < 2**31:
            result[key] = value
    return result


class Recorder:
    def __init__(self, command: str, started: float):
        self.common = _labels({"command": command})
        self.started = started
        self.startup = False
        self.agent_handed_off = False
        self.startup_labels: dict[str, Any] = {
            "interactive_login": False,
            "first_time_setup": False,
            "preflight_skipped": False,
        }
        self.phase = "command"
        self.workspace: str | None = None
        self.credentials: dict[str, tuple[str, dict[str, str]]] = {}
        self.pending: deque[tuple[str | None, str, float | int, dict]] = deque(
            maxlen=MAX_OBSERVATIONS
        )
        self.lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.reporter = None
        self.unsent = b""
        self.dirty = False
        self.credentials_dirty = True
        self.flush_requested = False

    def record(self, name: str, value: float | int, workspace: str | None, labels: dict) -> None:
        if name not in METRICS or not math.isfinite(value) or value < 0:
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.pending.append(
                (
                    destination(workspace) if workspace else self.workspace,
                    name,
                    value,
                    {**self.common, **_labels(labels)},
                )
            )
            self.dirty = True
            self._handoff_locked()
        finally:
            self.lock.release()
            if self.reporter is None or self.unsent:
                self.wake.set()

    def bind(self, workspace: str, token: str) -> None:
        from ucode.workspace import split_workspace_key, workspace_headers

        host, selected = split_workspace_key(workspace)
        headers = workspace_headers(host)
        if selected:
            from ucode.workspace import selector_headers

            headers = selector_headers(selected)
        key = destination(workspace)
        if not self.lock.acquire(blocking=False):
            return
        try:
            first_workspace = self.workspace is None
            self.workspace = key
            if len(self.credentials) >= 16 and key not in self.credentials:
                self.credentials.pop(next(iter(self.credentials)))
            self.credentials_dirty |= self.credentials.get(key) != (token, headers)
            self.credentials[key] = (token, headers)
            if first_workspace:
                self.pending = deque(
                    ((w or key, n, v, a) for w, n, v, a in self.pending), maxlen=MAX_OBSERVATIONS
                )
            self.dirty = True
            self._handoff_locked()
        finally:
            self.lock.release()
            if self.reporter is None or self.unsent:
                self.wake.set()

    def set_context(self, **labels: Any) -> None:
        self.common.update(_labels(labels))
        self.dirty = True
        if not self.lock.acquire(blocking=False):
            return
        try:
            for _, _, _, attrs in self.pending:
                if "command" in labels:
                    attrs["command"] = self.common["command"]
                if "agent" in labels and attrs.get("agent") in (None, "unknown"):
                    attrs["agent"] = self.common["agent"]
            self.dirty = True
            self._handoff_locked()
        finally:
            self.lock.release()
            if self.reporter is None or self.unsent:
                self.wake.set()

    def _write_locked(self) -> bool:
        assert self.reporter is not None
        try:
            written = os.write(self.reporter.stdin.fileno(), self.unsent)
            self.unsent = self.unsent[written:]
        except BlockingIOError:
            return False
        except OSError:
            self.reporter.stdin.close()
            self.unsent = b""
            return False
        return not self.unsent

    def _handoff_locked(self) -> None:
        if self.reporter is None or self.reporter.stdin.closed:
            return
        # Finish a partial frame before sending another; EOF discards an incomplete frame.
        if self.unsent and not self._write_locked():
            return
        if not self.dirty and not self.flush_requested:
            return
        message = {
            "observations": list(self.pending),
            "workspace": self.workspace,
            "context": self.common,
            "flush": self.flush_requested,
        }
        if self.credentials_dirty:
            message["credentials"] = self.credentials
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        self.pending.clear()
        self.dirty = False
        self.flush_requested = False
        if len(payload) > MAX_HANDOFF_BYTES:
            return
        self.credentials_dirty = False
        self.unsent = payload
        self._write_locked()

    def flush(self) -> None:
        self.start_periodic()
        if self.lock.acquire(blocking=False):
            try:
                self.flush_requested = True
                self._handoff_locked()
            finally:
                self.lock.release()
        if self.reporter is None or self.unsent:
            self.wake.set()

    def close(self) -> None:
        self.stop.set()
        if self.lock.acquire(blocking=False):
            try:
                self._handoff_locked()
                if self.reporter is not None:
                    self.reporter.stdin.close()
            finally:
                self.lock.release()
        self.wake.set()

    def start_periodic(self) -> None:
        if self.thread is not None or self.stop.is_set():
            return
        if not self.worker_lock.acquire(blocking=False):
            return
        try:
            if self.thread is None:
                thread = threading.Thread(target=self._export_loop, name="ug-metrics", daemon=True)
                thread.start()
                self.thread = thread
        finally:
            self.worker_lock.release()

    def _export_loop(self) -> None:
        from ucode.metrics.exporter import start_exporter

        try:
            while not self.stop.is_set():
                self.wake.clear()
                with suppress(Exception):
                    if self.reporter is None or self.reporter.poll() is not None:
                        if self.reporter is not None:
                            self.reporter.stdin.close()
                        reporter = start_exporter()
                        with self.lock:
                            self.reporter = reporter
                            self.unsent = b""
                            self.credentials_dirty = True
                            self.dirty = True
                            self._handoff_locked()
                    else:
                        with self.lock:
                            self._handoff_locked()
                if not self.stop.is_set():
                    # Retry backpressure only in the daemon, never on a command thread.
                    self.wake.wait(1 if self.unsent else None)
        finally:
            with self.lock:
                if self.reporter is not None:
                    self.reporter.stdin.close()


@_best_effort
def begin(command: str, started: float | None = None) -> None:
    global _runtime
    from ucode.env import env_bool

    if not env_bool("UG_ENABLE_METRICS_EXPORT"):
        return
    runtime = Recorder(command, time.perf_counter() if started is None else started)
    if command in _AGENT_SESSION_COMMANDS:
        runtime.phase = "agent_session"
    runtime.start_periodic()
    _runtime = runtime


@_best_effort
def set_command(command: str) -> None:
    runtime = _runtime
    if runtime is None:
        return
    runtime.phase = "agent_session" if command in _AGENT_SESSION_COMMANDS else "command"
    runtime.set_context(command=command)


@_best_effort
def set_context(**labels: Any) -> None:
    if _runtime is not None:
        _runtime.set_context(**labels)


def bind(workspace: str, token: str) -> None:
    if _runtime is not None:
        try:
            _runtime.bind(workspace, token)
        except Exception:
            pass


def record(name: str, value: float | int, *, workspace: str | None = None, **labels: Any) -> None:
    if _runtime is not None:
        try:
            current = _scope.get() or {}
            _runtime.record(name, value, workspace or current.get("workspace"), labels)
        except Exception:
            pass


@_best_effort
def annotate(**labels: Any) -> None:
    if current := _scope.get():
        current["labels"].update(_labels(labels))


def error_code(error: BaseException | str) -> str:
    # Categorize locally; exception messages, URLs, response bodies and prompts are never exported.
    text = str(error).lower()
    status = re.search(r"\bhttp (\d{3})\b", text)
    if status:
        return f"http_{status[1]}"
    if "timeout" in text or "timed out" in text or isinstance(error, TimeoutError):
        return "timeout"
    if "permission" in text or isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, OSError) or "network" in text:
        return "network_error"
    if "token" in text or "auth" in text or "login" in text or "oauth" in text:
        return "authentication_failed"
    if getattr(error, "returncode", None) is not None:
        return "process_failed"
    return "operation_failed"


def failure(
    operation: str,
    error: BaseException | str,
    *,
    continued_with_fallback: bool = False,
    **labels: Any,
) -> None:
    runtime = _runtime
    if runtime is None:
        return
    # Error classification must not replace an operation's result or original exception.
    with suppress(Exception):
        if isinstance(error, BaseException) and _is_cancellation(error):
            return
        if isinstance(error, SystemExit):
            if error.code in (None, 0):
                return
            # Child exit codes are propagated after handoff; spawn/UG errors still arrive as exceptions.
            if operation == COMMAND_OPERATION and runtime.agent_handed_off:
                return
        if operation == COMMAND_OPERATION:
            operation = str(runtime.common.get("command", COMMAND_OPERATION))
        current = _scope.get()
        if isinstance(error, str) and current is not None:
            code = error_code(error)
            if current.get("recorded_error") == code:
                return
            current["recorded_error"] = code
        if isinstance(error, BaseException):
            if already_reported(error):
                return
            # Tag the exception instead of retaining tracebacks (and their request bodies).
            vars(error)["_ug_metric_reported"] = True
            code = getattr(error, "returncode", None)
            if type(code) is int:
                labels["process_exit_code"] = code
        record(
            UG_CLI_ERROR,
            1,
            phase=labels.pop("phase", runtime.phase),
            operation=operation,
            error_code=error_code(error),
            continued_with_fallback=continued_with_fallback,
            **labels,
        )


@contextmanager
def measure(
    name: str,
    *,
    workspace: str | None = None,
    operation: str | None = None,
    continued_with_fallback: bool = False,
    **labels: Any,
) -> Iterator[dict[str, Any]]:
    values: dict[str, Any] = {}
    scope = None
    started = None
    with suppress(Exception):
        values = _labels(labels)
        if _runtime is not None:
            if workspace:
                workspace = destination(workspace)
            started = time.perf_counter()
            scope = _scope.set(
                {
                    "workspace": workspace,
                    "labels": values,
                    "fallback": continued_with_fallback,
                    "name": name,
                    "parent": _scope.get(),
                }
            )
    try:
        yield values
    except Exception as exc:
        if operation and scope is not None:
            failure(operation, exc, continued_with_fallback=continued_with_fallback)
        raise
    finally:
        with suppress(Exception):
            if scope is not None and started is not None:
                record(name, (time.perf_counter() - started) * 1000, workspace=workspace, **values)
        with suppress(Exception):
            if scope is not None:
                _scope.reset(scope)


def timed(
    name: str,
    *,
    label_args: dict[str, str] | None = None,
    reason_result: bool = False,
    continued_with_fallback: bool = False,
    **labels: Any,
):
    """Time an operation, including its retries; optional returned reasons are final failures."""

    def decorate(fn: Callable):
        signature = inspect.signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            if _runtime is None:
                return fn(*args, **kwargs)
            try:
                arguments = signature.bind(*args, **kwargs)
                arguments.apply_defaults()
                params = arguments.arguments
                state = params.get("state") or {}
                workspace = params.get("workspace") or state.get("workspace")
                token = params.get("token")
                if workspace and token:
                    bind(workspace, token)
                values = {
                    **labels,
                    **{label: params[arg] for arg, label in (label_args or {}).items()},
                }
                operation = name.removesuffix("_duration_ms")
            except Exception:
                return fn(*args, **kwargs)
            with measure(
                name,
                workspace=workspace,
                operation=operation,
                continued_with_fallback=continued_with_fallback,
                **values,
            ):
                result = fn(*args, **kwargs)
                with suppress(Exception):
                    if reason_result and isinstance(result, tuple) and isinstance(result[-1], str):
                        failure(
                            operation, result[-1], continued_with_fallback=continued_with_fallback
                        )
                return result

        return wrapped

    return decorate


@_best_effort
def mark_login() -> None:
    if _runtime is not None:
        _runtime.startup_labels["interactive_login"] = True


@_best_effort
def mark_interactive_wait() -> None:
    current = _scope.get()
    while current is not None:
        if current["name"] == DEPENDENCY_SETUP_DURATION_MS:
            current["labels"]["interactive_wait"] = True
        current = current["parent"]


@_best_effort
def start_agent(
    agent: str, *, preflight_skipped: bool = False, first_time_setup: bool = False
) -> None:
    runtime = _runtime
    if runtime is not None:
        runtime.startup = True
        runtime.phase = "startup"
        set_context(agent=agent)
        runtime.startup_labels.update(
            preflight_skipped=preflight_skipped, first_time_setup=first_time_setup
        )


@_best_effort
def agent_handoff() -> None:
    runtime = _runtime
    if runtime is not None and runtime.startup:
        record(
            UG_STARTUP_DURATION_MS,
            (time.perf_counter() - runtime.started) * 1000,
            **runtime.startup_labels,
        )
        runtime.startup = False
        runtime.agent_handed_off = True
        runtime.phase = "agent_session"
        flush()
        start_periodic()


def flush() -> None:
    if _runtime is not None:
        try:
            _runtime.flush()
        except Exception:
            pass


@_best_effort
def finish() -> None:
    global _runtime
    runtime = _runtime
    _runtime = None
    if runtime is None:
        return
    with suppress(Exception):
        if runtime.startup:
            runtime.record(
                UG_STARTUP_DURATION_MS,
                (time.perf_counter() - runtime.started) * 1000,
                None,
                runtime.startup_labels,
            )
    # Closing the nonblocking pipe lets the already-running helper flush after CLI exit.
    with suppress(Exception):
        runtime.flush()
    with suppress(Exception):
        runtime.close()


def api_operation(url: str) -> str:
    path = urlsplit(url).path.lower()
    for fragment, name in (
        ("coding-agent-config", "coding_agent_config_fetch"),
        ("budget", "budget_spend_statuses"),
        ("routes:select", "routes_select"),
        ("model-provider-services", "model_provider_services_list"),
        ("model-services", "model_services_list"),
        ("/messages", "messages"),
        ("/responses", "responses"),
        ("/chat/completions", "chat_completions"),
        ("/traces", "otel_traces"),
        ("/metrics", "otel_metrics"),
        ("/logs", "otel_logs"),
        ("/models", "models_list"),
        ("/skills", "skills"),
        ("/mcp", "mcp"),
        ("/scim/", "scim_me"),
        ("/apps", "apps_list"),
    ):
        if fragment in path:
            return name
    return "other"


def start_periodic() -> None:
    if _runtime is not None:
        try:
            _runtime.start_periodic()
        except Exception:
            pass


def destination(workspace: str) -> str:
    from ucode.workspace import split_workspace_key, workspace_headers, workspace_key

    host, selected = split_workspace_key(workspace)
    return workspace_key(host, selected or workspace_headers(host).get("X-Databricks-Workspace-Id"))


def _is_cancellation(error: BaseException | None) -> bool:
    # Click can wrap KeyboardInterrupt in Abort and then SystemExit(1).
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (
            isinstance(error, KeyboardInterrupt)
            or getattr(error, "exit_code", None) == 130
            or (isinstance(error, SystemExit) and error.code == 130)
        ):
            return True
        error = error.__cause__ or error.__context__
    return False


def already_reported(error: BaseException | None) -> bool:
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "_ug_metric_reported", False):
            return True
        error = error.__cause__ or error.__context__
    return False


_routing_trigger = ContextVar("ug_routing_trigger", default="first_prompt")


def routing_trigger() -> str:
    return _routing_trigger.get()


@contextmanager
def subagent_routing() -> Iterator[None]:
    token = _routing_trigger.set("subagent")
    try:
        yield
    finally:
        _routing_trigger.reset(token)


_http_attempt: ContextVar[int | None] = ContextVar("ug_http_attempt", default=None)


@contextmanager
def http_attempt(attempt: int) -> Iterator[None]:
    token = _http_attempt.set(attempt)
    try:
        yield
    finally:
        _http_attempt.reset(token)


def attempt_number(default: int) -> int:
    return _http_attempt.get() or default


def http_result(fn: Callable):
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        result = fn(*args, **kwargs)
        with suppress(Exception):
            if _runtime is not None and result[-1] and _http_attempt.get() is None:
                params = signature.bind(*args, **kwargs).arguments
                current = _scope.get() or {}
                failure(
                    api_operation(params["url"]),
                    result[-1],
                    continued_with_fallback=current.get("fallback", False),
                )
        return result

    return wrapped
