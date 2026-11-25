"""Model-list validation against a loopback Smart Router, through the real HTTP transport."""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import tls_ca
from tests.conftest import metric_observations
from ucode import databricks, metrics
from ucode.constants import AGENT_CODEX, ROUTER_HARNESS_CLAUDE_CODE
from ucode.smart_routing import route_validation as validation
from ucode.smart_routing.session_env import session_routing_enabled, validation_result_path
from ucode.workspace import activate

UNREACHABLE = "unreachable"
UNEXPECTED_EXCEPTION = "unexpected-exception"


class _Router:
    status = 200
    body = b'{"is_supported": true}'
    pace = 0
    header_delay = 0
    url = None
    closed_url = None

    def __init__(self):
        self.requests = []
        self.answered = threading.Event()


def _handler(router):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            router.requests.append((self.path, self.headers))
            time.sleep(router.header_delay)
            self.send_response(router.status)
            self.send_header("Content-Length", str(len(router.body)))
            self.send_header("x-request-id", "test-request-id")
            self.end_headers()
            for byte in router.body:
                self.wfile.write(bytes([byte]))
                time.sleep(router.pace)
            router.answered.set()

        def log_message(self, *args):
            pass

    return Handler


def _closed_url():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


@pytest.fixture
def router():
    state = _Router()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.closed_url = _closed_url()
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(autouse=True)
def real_transport(monkeypatch):
    """Replaces conftest's stub so the check sends its request over the loopback socket."""
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(validation, "_http_post_json", databricks._http_post_json)


def _wait_for_result(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail("validation worker did not publish a result")


def _run_check(workspace, tmp_path, available_harness=ROUTER_HARNESS_CLAUDE_CODE):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        workspace,
        "test-token",
        [("custom.model", "claude")],
        router_name="stable",
        session_controls=controls,
        available_harness=available_harness,
    )
    return controls, _wait_for_result(validation_result_path(controls))


def _failed(reason):
    return {
        "routing_disabled": True,
        "router_name": "stable",
        "validation_failed": True,
        "validation_failure_reason": reason,
        "eligible_route_options": [],
        "ineligible_route_options": [],
        "available_route_options": [],
    }


@pytest.mark.parametrize(
    "reply,failure,error",
    [
        pytest.param((200, b'{"is_supported": true}'), None, None, id="supported"),
        pytest.param(
            (404, b'{"error_code": "NOT_FOUND"}'),
            None,
            ("routes_validate", "http_404"),
            id="flag-off-404",
        ),
        pytest.param(
            (400, b"bad request"),
            "Smart Router returned an error (HTTP 400)",
            ("routes_validate", "http_400"),
            id="http-400",
        ),
        pytest.param(
            (403, b"forbidden"),
            "Smart Router returned an error (HTTP 403)",
            ("routes_validate", "http_403"),
            id="http-403",
        ),
        pytest.param(
            (500, b"boom"),
            "Smart Router returned an error (HTTP 500)",
            ("routes_validate", "http_500"),
            id="http-500",
        ),
        pytest.param(
            (500, b'{"error_code": "NOT_FOUND"}'),
            "Smart Router returned an error (HTTP 500)",
            ("routes_validate", "http_500"),
            id="http-500-not-found-body",
        ),
        pytest.param(
            UNREACHABLE,
            "Smart Router couldn't be reached",
            ("routes_validate", "network_error"),
            id="unreachable",
        ),
        pytest.param(
            (200, b"<html>sign in</html>"),
            "Smart Router returned an unexpected response",
            ("routes_validate", "operation_failed"),
            id="not-json",
        ),
        pytest.param(
            (200, b"[]"), "Smart Router returned an unexpected response", None, id="not-object"
        ),
        pytest.param(
            (200, b"{}"),
            "Smart Router returned an unexpected response",
            None,
            id="missing-is-supported",
        ),
        pytest.param(
            (200, b'{"is_supported": "false"}'),
            "Smart Router returned an unexpected response",
            None,
            id="non-boolean-is-supported",
        ),
        pytest.param(
            UNEXPECTED_EXCEPTION,
            "Smart Router check failed unexpectedly",
            ("route_validation", "operation_failed"),
            id="exception",
        ),
    ],
)
def test_failed_check_turns_routing_off_except_for_a_flag_off_404(
    reply, failure, error, tmp_path, router, metrics_recorder
):
    if reply == UNREACHABLE:
        workspace = router.closed_url
    elif reply == UNEXPECTED_EXCEPTION:
        # An empty host makes workspace_origin raise inside the probe.
        workspace = "https://"
    else:
        router.status, router.body = reply
        workspace = router.url
    controls, result = _run_check(workspace, tmp_path)
    assert len(metric_observations(metrics_recorder, metrics.ROUTE_VALIDATION_DURATION_MS)) == 1
    errors = metric_observations(metrics_recorder, metrics.UG_CLI_ERROR)
    assert [
        (labels["operation"], labels["error_code"], labels["continued_with_fallback"])
        for _, labels in errors
    ] == ([] if error is None else [(*error, True)])
    if failure is None:
        assert result == {"routing_disabled": False}
        assert session_routing_enabled(controls) is True
        assert controls.read_text() == "{}"
        return
    assert isinstance(result.pop("validation_ms"), int)
    assert result == _failed(failure)
    assert session_routing_enabled(controls) is False


def test_request_uses_existing_http_auth_and_workspace_selection(tmp_path, router):
    activate(router.url, "111")
    controls = tmp_path / "env.json"
    validation.start(
        f"{router.url}?w=111",
        "test-token",
        [("original.model", "claude")],
        router_name="stable",
        session_controls=controls,
        provider="main.default.mps",
    )
    assert _wait_for_result(validation_result_path(controls)) == {"routing_disabled": False}
    [(path, headers)] = router.requests
    assert path == "/ai-gateway/routing/v1/routes:validate"
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["X-Databricks-Workspace-Id"] == "111"
    assert headers["X-Databricks-Org-Id"] == "111"
    assert headers["Databricks-model-provider-service"] == "main.default.mps"


def test_a_check_past_the_deadline_publishes_a_timeout_that_a_late_result_cannot_overwrite(
    tmp_path, monkeypatch, router, metrics_recorder
):
    """The body trickles in slower than the deadline but faster than the socket timeout, so only
    the deadline ends the wait and the late body completes afterwards."""
    monkeypatch.setattr(validation, "VALIDATION_TIMEOUT_SECONDS", 0.2)
    router.pace = 0.02
    router.header_delay = 0.05
    router.body = b'{"is_supported": true}' + b" " * 30
    controls, result = _run_check(router.url, tmp_path)
    assert [
        (labels["operation"], labels["error_code"])
        for _, labels in metric_observations(metrics_recorder, metrics.UG_CLI_ERROR)
    ] == [("route_validation", "timeout")]
    assert len(metric_observations(metrics_recorder, metrics.ROUTE_VALIDATION_DURATION_MS)) == 1
    assert router.answered.wait(3)
    assert json.loads(validation_result_path(controls).read_text()) == result
    assert isinstance(result.pop("validation_ms"), int)
    diagnostics = result.pop("validation_diagnostics")
    assert result == _failed("Smart Router didn't respond in time")
    for detail in ("connect_dns_tcp=", "tls=", "ttfb=", "request_id="):
        assert detail in diagnostics, diagnostics
    assert session_routing_enabled(controls) is False


def test_router_verdict_keeps_untrusted_reason_separate_from_diagnostics():
    reason = "network error: timed out; diagnostics: synthetic-secret"
    result = validation._verdict(
        None, reason, "stable", time.monotonic(), "codex", diagnostics="connect_dns_tcp=42ms"
    )
    assert result["validation_failure_reason"] == "Smart Router didn't respond in time"
    assert result["validation_diagnostics"] == "connect_dns_tcp=42ms"
    assert "synthetic-secret" not in json.dumps(result)


def test_http_error_with_timeout_text_does_not_become_a_timeout_diagnostic():
    result = validation._verdict(
        None,
        "HTTP 504: upstream timed out",
        "stable",
        time.monotonic(),
        "codex",
        diagnostics="connect_dns_tcp=42ms",
    )
    assert result["validation_failure_reason"] == "Smart Router returned an error (HTTP 504)"
    assert "validation_diagnostics" not in result


def test_timeout_diagnostics_exclude_untrusted_reason_text():
    reason = (
        "network error: timed out; diagnostics: synthetic-secret; diagnostics: connect_dns_tcp=42ms"
    )
    result = validation._verdict(None, reason, "stable", time.monotonic(), "codex")
    assert result["validation_failure_reason"] == "Smart Router didn't respond in time"
    assert "validation_diagnostics" not in result
    assert "synthetic-secret" not in json.dumps(result)
    plain = validation._verdict(
        None, "network error: timed out", "stable", time.monotonic(), "codex"
    )
    assert "validation_diagnostics" not in plain


def test_validation_probe_does_not_install_a_global_profiler(router, monkeypatch):
    monkeypatch.setattr(
        sys, "setprofile", lambda *_: pytest.fail("global profiling is not allowed")
    )
    timings = validation._NetworkTimings()
    outcome = {}
    validation._probe(router.url, "test-token", {"route_options": []}, None, outcome, timings)
    assert outcome["response"] == ({"is_supported": True}, None)


def test_validation_probe_preserves_response_when_diagnostics_raise(router, monkeypatch):
    monkeypatch.setattr(databricks, "_use_pooled_client", lambda: True)
    timings = validation._NetworkTimings()

    def broken_trace(_event, _info):
        raise RuntimeError("synthetic diagnostic failure")

    monkeypatch.setattr(timings, "trace", broken_trace)
    outcome = {}
    validation._probe(router.url, "test-token", {"route_options": []}, None, outcome, timings)
    assert outcome["response"] == ({"is_supported": True}, None)


def test_validation_probe_restores_request_trace_after_success_and_failure(router, monkeypatch):
    monkeypatch.setattr(databricks, "_use_pooled_client", lambda: True)
    events = []

    def previous(event, _info):
        events.append(event)

    token = databricks._HTTP_TRACE.set(previous)
    try:
        outcome = {}
        validation._probe(
            router.url,
            "test-token",
            {"route_options": []},
            None,
            outcome,
            validation._NetworkTimings(),
        )
        assert outcome["response"] == ({"is_supported": True}, None)
        assert databricks._HTTP_TRACE.get() is previous
        assert events == []
        payload, reason = databricks._http_post_json(
            f"{router.url}/unrelated", "test-token", {}, timeout=4
        )
        assert payload == {"is_supported": True} and reason is None
        assert events

        def fail(*_args):
            raise RuntimeError("synthetic transport failure")

        monkeypatch.setattr(validation, "_validate_route_options", fail)
        validation._probe(router.url, "test-token", {}, None, outcome, validation._NetworkTimings())
        assert outcome["response"] == (None, validation.CHECK_FAILED_REASON)
        assert databricks._HTTP_TRACE.get() is previous
    finally:
        databricks._HTTP_TRACE.reset(token)


def test_network_trace_records_real_loopback_transport(router, monkeypatch):
    monkeypatch.setattr(databricks, "_use_pooled_client", lambda: True)
    timings = validation._NetworkTimings()
    token = databricks._HTTP_TRACE.set(timings.trace)
    try:
        payload, reason = databricks._http_post_json(
            f"{router.url}/validate", "test-token", {"models": ["m"]}, timeout=4
        )
    finally:
        databricks._HTTP_TRACE.reset(token)
    assert payload == {"is_supported": True} and reason is None
    summary = timings.summary()
    for phase in ("connect_dns_tcp", "ttfb"):
        assert re.search(rf"{phase}=\d+ms", summary), summary
    assert "transport=pooled" in summary
    assert "connection=new" in summary
    assert "request_id=test-request-id" in summary


@pytest.mark.parametrize("status", [400, 500])
def test_urllib_error_response_preserves_request_id(router, monkeypatch, status):
    monkeypatch.setattr(databricks, "_use_pooled_client", lambda: False)
    router.status = status
    timings = validation._NetworkTimings()
    token = databricks._HTTP_TRACE.set(timings.trace)
    try:
        payload, reason = databricks._http_post_json(
            f"{router.url}/validate", "test-token", {}, timeout=4
        )
    finally:
        databricks._HTTP_TRACE.reset(token)
    assert payload is None and f"HTTP {status}" in reason
    assert "request_id=test-request-id" in timings.summary()


def test_network_trace_records_real_connection_closed_before_response(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.close_connection = True

        def log_message(self, *_args):
            pass

    monkeypatch.setattr(databricks, "_use_pooled_client", lambda: True)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        timings = validation._NetworkTimings()
        token = databricks._HTTP_TRACE.set(timings.trace)
        try:
            payload, reason = databricks._http_post_json(
                f"http://127.0.0.1:{server.server_port}/validate", "test-token", {}, timeout=4
            )
        finally:
            databricks._HTTP_TRACE.reset(token)
            server.shutdown()
            thread.join()
    assert payload is None and reason
    assert "ttfb_source=headers-failed" in timings.summary()


@pytest.mark.parametrize("disable_pool", [True, False], ids=["urllib", "httpcore"])
def test_network_trace_records_https_transport(tmp_path, disable_pool):
    ca = tmp_path / "ca"
    tls_ca.write_ca(ca)
    certificate = tmp_path / "localhost.pem"
    tls_ca.write_localhost_certificate(ca, certificate)
    router = _Router()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate)
    script = """
import json, sys
from ucode import databricks
from ucode.smart_routing.route_validation import _NetworkTimings
trace = _NetworkTimings()
token = databricks._HTTP_TRACE.set(trace.trace)
try:
    payload, reason = databricks._http_post_json(sys.argv[1], 'test-token', {}, timeout=4)
finally:
    databricks._HTTP_TRACE.reset(token)
print(json.dumps({'payload': payload, 'reason': reason, 'summary': trace.summary()}))
"""
    with ThreadingHTTPServer(("127.0.0.1", 0), _handler(router)) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            env = {
                **os.environ,
                "HOME": str(tmp_path),
                "USERPROFILE": str(tmp_path),
                "UG_NO_POOLED_HTTP": "1" if disable_pool else "0",
                "SSL_CERT_FILE": str(ca / tls_ca.CA_CERTIFICATE),
                "NO_PROXY": "localhost,127.0.0.1",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "http_proxy": "",
                "https_proxy": "",
            }
            env.pop("UG_TLS_TRUST_STORE", None)
            result = subprocess.run(
                [sys.executable, "-c", script, f"https://127.0.0.1:{server.server_port}/validate"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
        finally:
            server.shutdown()
            worker.join(timeout=2)
    output = json.loads(result.stdout)
    assert output["payload"] == {"is_supported": True} and output["reason"] is None
    summary = output["summary"]
    if disable_pool:
        assert "transport=urllib" in summary
        assert "connect_dns_tcp=not-observed" in summary
        assert "tls=not-observed" in summary
        assert "ttfb=not-observed" in summary
    else:
        assert "transport=pooled" in summary
        for phase in ("connect_dns_tcp", "tls", "ttfb"):
            assert re.search(rf"{phase}=\d+ms", summary), summary
    assert "request_id=test-request-id" in summary


def test_network_trace_records_request_id_and_connection_reuse():
    timings = validation._NetworkTimings()
    timings.trace("transport.pooled", {})
    timings.trace("http11.send_request_headers.started", {})
    timings.trace("http11.receive_response_headers.started", {})
    timings.trace(
        "http11.receive_response_headers.complete",
        {"return_value": (b"HTTP/1.1", 200, b"OK", [(b"x-request-id", b"test-request-id")])},
    )
    reason = timings.summary()
    assert "transport=pooled" in reason
    assert "connection=reused" in reason
    assert "request_id=test-request-id" in reason
    assert re.search(r"ttfb=\d+ms", reason)
    assert "ttfb_source=headers-complete" in reason
    timings.trace("response.headers", {"headers": [("request-id", "request-2")]})
    assert "request_id=request-2" in timings.summary()
    timings.trace("response.headers", {"headers": [("x-databricks-request-id", "request-3")]})
    assert "request_id=request-3" in timings.summary()
    timings.trace("response.headers", {"headers": [("x-request-id", "Bearer secret")]})
    assert "Bearer secret" not in timings.summary()


def test_network_trace_distinguishes_failed_response_headers():
    timings = validation._NetworkTimings()
    timings.trace("http11.receive_response_headers.started", {})
    timings.trace("http11.receive_response_headers.failed", {})
    summary = timings.summary()
    assert re.search(r"ttfb=\d+ms", summary)
    assert "ttfb_source=headers-failed" in summary
    assert "ttfb_source=headers-complete" not in summary


def test_network_trace_prefers_same_safe_request_id_as_forwarder():
    timings = validation._NetworkTimings()
    timings.trace(
        "response.headers",
        {"headers": [(b"x-request-id", b"preferred-id"), (b"request-id", b"secondary-id")]},
    )
    assert "request_id=preferred-id" in timings.summary()
    invalid = validation._NetworkTimings()
    invalid.trace("response.headers", {"headers": [(b"x-request-id", b"\xffsecret")]})
    assert "request_id=unavailable" in invalid.summary()
    invalid.trace(
        "response.headers",
        {"headers": [(b"x-request-id", b"\xffsecret"), (b"request-id", b"safe-id")]},
    )
    assert "request_id=safe-id" in invalid.summary()
    assert "secret" not in invalid.summary()


class _SynchronousThread:
    def __init__(self, *, target, args=(), daemon):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)

    def join(self, timeout=None):
        pass


@pytest.mark.parametrize("failing", ["result", "controls"])
def test_failed_write_is_isolated_and_keeps_the_result_honest(
    failing, tmp_path, monkeypatch, router
):
    # start() returns no handle; an inline worker lets the test assert the publish without sleeping.
    monkeypatch.setattr(validation, "threading", SimpleNamespace(Thread=_SynchronousThread))
    router.body = b'{"is_supported": false}'
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    if failing == "result":
        validation_result_path(controls).mkdir()
    else:
        controls.unlink()
        controls.mkdir()
    validation.start(
        router.url,
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    if failing == "result":
        assert validation_result_path(controls).is_dir()
        assert session_routing_enabled(controls) is False
    else:
        assert json.loads(validation_result_path(controls).read_text()) == {
            "routing_disabled": False
        }
        assert controls.is_dir()


@pytest.mark.parametrize("harness", [ROUTER_HARNESS_CLAUDE_CODE, AGENT_CODEX])
def test_unsupported_result_carries_the_details_for_the_logs_row(harness, tmp_path, router):
    payload = {
        "is_supported": False,
        "metadata": {"routing_unsupported_reason": "insufficient_eligible_options"},
        "eligible_route_options": [{"model": "system.ai.claude-sonnet", "harness": "claude"}],
        "ineligible_route_options": [
            {
                "model": "system.ai.claude-haiku-4-5",
                "harness": "claude",
                "reason": "model_not_in_recipe",
            },
            {"model": "custom", "harness": "claude"},
            "malformed",
        ],
        "available_route_options": [
            {"model": "claude-sonnet-5-5", "harness": "claude_code"},
            {"model": "claude-sonnet-5-5", "harness": "codex"},
        ],
    }
    router.body = json.dumps(payload).encode()
    _, result = _run_check(router.url, tmp_path, available_harness=harness)
    assert isinstance(result.pop("validation_ms"), int)
    assert result == {
        "routing_disabled": True,
        "router_name": "stable",
        "routing_unsupported_reason": "insufficient_eligible_options",
        "eligible_route_options": [{"model": "system.ai.claude-sonnet", "harness": "claude"}],
        "ineligible_route_options": [
            {
                "model": "system.ai.claude-haiku-4-5",
                "harness": "claude",
                "reason": "model_not_in_recipe",
            },
            {"model": "custom", "harness": "claude", "reason": None},
        ],
        "available_route_options": [{"model": "claude-sonnet-5-5", "harness": harness}],
    }


def _validate(response, monkeypatch, available_harness=ROUTER_HARNESS_CLAUDE_CODE):
    monkeypatch.setattr(
        validation,
        "_http_post_json",
        Mock(side_effect=response)
        if isinstance(response, Exception)
        else Mock(return_value=response),
    )
    return validation.validate(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        available_harness=available_harness,
    )


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        ((None, "HTTP 503"), "Smart Router returned an error (HTTP 503)"),
        (RuntimeError("network down"), validation.CHECK_FAILED_REASON),
    ],
)
def test_validate_disables_routing_when_the_toggle_check_fails(response, failure, monkeypatch):
    result = _validate(response, monkeypatch)
    assert isinstance(result.pop("validation_ms"), int)
    assert result == {
        "routing_disabled": True,
        "router_name": "stable",
        "validation_failed": True,
        "validation_failure_reason": failure,
        "eligible_route_options": [],
        "ineligible_route_options": [],
        "available_route_options": [],
    }


@pytest.mark.parametrize(
    "response",
    [({"is_supported": True}, None), (None, "HTTP 404 NOT_FOUND")],
    ids=["supported", "flag-off-404"],
)
def test_validate_keeps_routing_on_for_a_supported_list_or_a_flag_off_404(response, monkeypatch):
    assert _validate(response, monkeypatch) == {"routing_disabled": False}


def test_validate_disables_routing_with_the_unsupported_details(monkeypatch):
    payload = {
        "is_supported": False,
        "metadata": {"routing_unsupported_reason": "insufficient_eligible_options"},
    }
    result = _validate((payload, None), monkeypatch)
    assert isinstance(result.pop("validation_ms"), int)
    assert result == {
        "routing_disabled": True,
        "router_name": "stable",
        "routing_unsupported_reason": "insufficient_eligible_options",
        "eligible_route_options": [],
        "ineligible_route_options": [],
        "available_route_options": [],
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({}, True),
        ({"ISAAC_LAUNCH_MODE": "omni"}, False),
    ],
)
def test_enabled_skips_omnigent(source, expected):
    assert validation.is_route_validation_enabled(source) is expected
