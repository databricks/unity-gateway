"""Bounded performance observations; OTel aggregation and uploads run out of process."""

from __future__ import annotations

import inspect
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

MAX_OBSERVATIONS = 1000
EXPORT_INTERVAL_SECONDS = 60
DURATION_METRICS = frozenset(
    {
        "ug_startup_duration_ms",
        "cli_import_duration_ms",
        "dependency_setup_duration_ms",
        "dependency_install_duration_ms",
        "databricks_token_fetch_duration_ms",
        "databricks_login_duration_ms",
        "coding_agent_config_fetch_duration_ms",
        "model_service_access_check_duration_ms",
        "model_service_discovery_duration_ms",
        "agent_configuration_duration_ms",
        "skills_refresh_duration_ms",
        "routing_decision_duration_ms",
        "ug_usage_duration_ms",
        "http_request_duration_ms",
        "proxy_upstream_headers_duration_ms",
        "proxy_first_byte_duration_ms",
        "proxy_request_duration_ms",
    }
)
COUNTER_METRICS = frozenset({"http_retry_count", "proxy_auth_retry_count", "ug_cli_error"})
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
        self.export_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.reporter = None

    def record(self, name: str, value: float | int, workspace: str | None, labels: dict) -> None:
        if name not in METRICS or not math.isfinite(value) or value < 0:
            return
        with self.lock:
            self.pending.append(
                (
                    destination(workspace) if workspace else self.workspace,
                    name,
                    value,
                    {**self.common, **_labels(labels)},
                )
            )

    def bind(self, workspace: str, token: str) -> None:
        # The bearer is already available from the operation being measured. Never log in for telemetry.
        from ucode.workspace import split_workspace_key, workspace_headers

        host, selected = split_workspace_key(workspace)
        headers = workspace_headers(host)
        if selected:
            from ucode.workspace import selector_headers

            headers = selector_headers(selected)
        key = destination(workspace)
        with self.lock:
            first_workspace = self.workspace is None
            self.workspace = key
            if len(self.credentials) >= 16 and key not in self.credentials:
                self.credentials.pop(next(iter(self.credentials)))
            self.credentials[key] = (token, headers)
            # Startup observations made before workspace selection belong to the first selected workspace.
            if first_workspace:
                self.pending = deque(
                    ((w or key, n, v, a) for w, n, v, a in self.pending), maxlen=MAX_OBSERVATIONS
                )

    def flush(self) -> None:
        from ucode.metrics_export import send_in_background

        # A stalled reporter must not produce unbounded children or a second export queue.
        if not self.export_lock.acquire(blocking=False):
            return
        try:
            if self.reporter is not None and self.reporter.poll() is None:
                return
            with self.lock:
                observations = list(self.pending)
                self.pending.clear()
                credentials = dict(self.credentials)
            self.reporter = send_in_background(observations, credentials)
        finally:
            self.export_lock.release()

    def start_periodic(self) -> None:
        if self.thread is not None:
            return

        def export_loop():
            while not self.stop.wait(EXPORT_INTERVAL_SECONDS):
                flush()

        self.thread = threading.Thread(target=export_loop, name="ug-metrics", daemon=True)
        self.thread.start()


def begin(command: str, started: float | None = None) -> None:
    global _runtime
    from ucode.env import env_bool

    if not env_bool("UG_ENABLE_METRICS_EXPORT"):
        return
    _runtime = Recorder(command, time.perf_counter() if started is None else started)
    if command in {
        "auth-token",
        "otel-proxy",
        "otel-headers",
        "mcp-proxy",
        "claude-router-hook",
        "codex-router-hook",
    }:
        _runtime.phase = "agent_session"


def set_context(**labels: Any) -> None:
    if _runtime is not None:
        _runtime.common.update(_labels(labels))


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
    if "token" in text or "auth" in text or "login" in text or "oauth" in text:
        return "authentication_failed"
    if isinstance(error, OSError) or "network" in text:
        return "network_error"
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
    if _runtime is None:
        return
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
        "ug_cli_error",
        1,
        phase=labels.pop("phase", _runtime.phase),
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
    values = _labels(labels)
    if workspace and _runtime is not None:
        workspace = destination(workspace)
    if _runtime is None:
        yield values
        return
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
        if operation:
            failure(operation, exc, continued_with_fallback=continued_with_fallback)
        raise
    finally:
        record(name, (time.perf_counter() - started) * 1000, workspace=workspace, **values)
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
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            params = arguments.arguments
            state = params.get("state") or {}
            workspace = params.get("workspace") or state.get("workspace")
            token = params.get("token")
            if workspace and token:
                bind(workspace, token)
            values = {**labels, **{label: params[arg] for arg, label in (label_args or {}).items()}}
            operation = name.removesuffix("_duration_ms")
            with measure(
                name,
                workspace=workspace,
                operation=operation,
                continued_with_fallback=continued_with_fallback,
                **values,
            ):
                result = fn(*args, **kwargs)
                if reason_result and isinstance(result, tuple) and isinstance(result[-1], str):
                    failure(operation, result[-1], continued_with_fallback=continued_with_fallback)
                return result

        return wrapped

    return decorate


def mark_login() -> None:
    if _runtime is not None:
        _runtime.startup_labels["interactive_login"] = True


def mark_interactive_wait() -> None:
    current = _scope.get()
    while current is not None:
        if current["name"] == "dependency_setup_duration_ms":
            current["labels"]["interactive_wait"] = True
        current = current["parent"]


def start_agent(
    agent: str, *, preflight_skipped: bool = False, first_time_setup: bool = False
) -> None:
    if _runtime is not None:
        _runtime.startup = True
        _runtime.phase = "startup"
        set_context(agent=agent)
        with _runtime.lock:
            for _, _, _, attrs in _runtime.pending:
                if attrs.get("agent") in (None, "unknown"):
                    attrs["agent"] = agent
        _runtime.startup_labels.update(
            preflight_skipped=preflight_skipped, first_time_setup=first_time_setup
        )


def agent_handoff() -> None:
    if _runtime is not None and _runtime.startup:
        record(
            "ug_startup_duration_ms",
            (time.perf_counter() - _runtime.started) * 1000,
            **_runtime.startup_labels,
        )
        _runtime.startup = False
        _runtime.phase = "agent_session"
        flush()
        start_periodic()


def flush() -> None:
    if _runtime is not None:
        try:
            _runtime.flush()
        except Exception:
            pass


def finish() -> None:
    global _runtime
    if _runtime is not None:
        if _runtime.startup:
            record(
                "ug_startup_duration_ms",
                (time.perf_counter() - _runtime.started) * 1000,
                **_runtime.startup_labels,
            )
        _runtime.stop.set()
        flush()
        _runtime = None


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
