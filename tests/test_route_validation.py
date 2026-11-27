"""Model-list validation against a loopback Smart Router, through the real HTTP transport."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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
            self.send_response(router.status)
            self.send_header("Content-Length", str(len(router.body)))
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
    assert result == _failed("Smart Router didn't respond in time")
    assert session_routing_enabled(controls) is False


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
