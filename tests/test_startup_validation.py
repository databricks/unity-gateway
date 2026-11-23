"""Startup validation with controlled HTTP and real background workers."""

from __future__ import annotations

import io
import json
import time
import urllib.error as urllib_error
from email.message import Message

import pytest

from tests.conftest import metric_observations
from ucode import databricks, metrics
from ucode.smart_routing import startup_validation as validation
from ucode.smart_routing.session_env import session_routing_enabled
from ucode.workspace import activate


class _Response(io.BytesIO):
    status = 200
    headers = Message()


def _http_error(status, body=b""):
    return urllib_error.HTTPError(
        "https://example.com", status, "error", Message(), io.BytesIO(body)
    )


def _wait_for_result(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail("validation worker did not publish a result")


def _run_check(reply, tmp_path, monkeypatch):
    """Run the check with the HTTP transport answering `reply` (a body, or an exception to raise)."""

    def open_request(request, *, timeout):
        if isinstance(reply, BaseException):
            raise reply
        return _Response(reply)

    monkeypatch.setattr(validation, "_http_post_json", databricks._http_post_json)
    monkeypatch.setattr(databricks, "_http_open", open_request)
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        "https://example.com",
        "test-token",
        [("custom.model", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    return controls, _wait_for_result(validation.result_path(controls))


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
    "reply,failure",
    [
        pytest.param(b'{"is_supported": true}', None, id="supported"),
        pytest.param(_http_error(404, b'{"error_code": "NOT_FOUND"}'), None, id="flag-off-404"),
        pytest.param(
            _http_error(400, b"bad request"),
            "Smart Router returned an error (HTTP 400)",
            id="http-400",
        ),
        pytest.param(
            _http_error(403, b"forbidden"),
            "Smart Router returned an error (HTTP 403)",
            id="http-403",
        ),
        pytest.param(
            _http_error(500, b"boom"),
            "Smart Router returned an error (HTTP 500)",
            id="http-500",
        ),
        pytest.param(
            _http_error(500, b'{"error_code": "NOT_FOUND"}'),
            "Smart Router returned an error (HTTP 500)",
            id="http-500-not-found-body",
        ),
        pytest.param(
            TimeoutError("timed out"), "Smart Router didn't respond in time", id="timeout"
        ),
        pytest.param(
            urllib_error.URLError(ConnectionRefusedError(111, "refused")),
            "Smart Router couldn't be reached",
            id="unreachable",
        ),
        pytest.param(
            b"<html>sign in</html>", "Smart Router returned an unexpected response", id="not-json"
        ),
        pytest.param(b"[]", "Smart Router returned an unexpected response", id="not-object"),
        pytest.param(
            b"{}", "Smart Router returned an unexpected response", id="missing-is-supported"
        ),
        pytest.param(
            b'{"is_supported": "false"}',
            "Smart Router returned an unexpected response",
            id="non-boolean-is-supported",
        ),
        pytest.param(
            RuntimeError("boom"), "Smart Router check failed unexpectedly", id="exception"
        ),
    ],
)
def test_failed_check_turns_routing_off_except_for_a_flag_off_404(
    reply, failure, tmp_path, monkeypatch, metrics_recorder
):
    controls, result = _run_check(reply, tmp_path, monkeypatch)
    assert len(metric_observations(metrics_recorder, metrics.ROUTE_VALIDATION_DURATION_MS)) == 1
    if failure is None:
        assert result == {"routing_disabled": False}
        assert session_routing_enabled(controls) is True
        assert controls.read_text() == "{}"
        return
    assert isinstance(result.pop("validation_ms"), int)
    assert result == _failed(failure)
    assert session_routing_enabled(controls) is False


def test_flag_off_404_keeps_the_fallback_metric(tmp_path, monkeypatch, metrics_recorder):
    _run_check(_http_error(404, b'{"error_code": "NOT_FOUND"}'), tmp_path, monkeypatch)
    errors = metric_observations(metrics_recorder, metrics.UG_CLI_ERROR)
    assert [
        (labels["operation"], labels["error_code"], labels["continued_with_fallback"])
        for _, labels in errors
    ] == [("routes_validate", "http_404", True)]


def test_request_uses_existing_http_auth_and_workspace_selection(tmp_path, monkeypatch):
    captured = []
    activate("https://example.com", "111")

    class Response(io.BytesIO):
        status = 200
        headers = Message()

    def urlopen(request, **kwargs):
        captured.append(request)
        return Response(b'{"is_supported":true}')

    monkeypatch.setattr(validation, "_http_post_json", databricks._http_post_json)
    monkeypatch.setattr(databricks, "_http_open", urlopen)
    controls = tmp_path / "env.json"
    validation.start(
        "https://example.com?w=111",
        "test-token",
        [("original.model", "claude")],
        router_name="stable",
        session_controls=controls,
        provider="main.default.mps",
    )
    assert _wait_for_result(validation.result_path(controls)) == {"routing_disabled": False}
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "https://example.com/ai-gateway/routing/v1/routes:validate"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("X-databricks-workspace-id") == "111"
    assert request.get_header("X-databricks-org-id") == "111"
    assert request.get_header("Databricks-model-provider-service") == "main.default.mps"


def test_a_check_past_the_deadline_publishes_a_timeout_and_turns_routing_off(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, "VALIDATION_TIMEOUT_SECONDS", 0.05)

    def slow_response(request, *, timeout):
        time.sleep(0.5)
        return _Response(b'{"is_supported": true}')

    monkeypatch.setattr(validation, "_http_post_json", databricks._http_post_json)
    monkeypatch.setattr(databricks, "_http_open", slow_response)
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    result = _wait_for_result(validation.result_path(controls))
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
def test_failed_write_is_isolated_and_keeps_the_result_honest(failing, tmp_path, monkeypatch):
    monkeypatch.setattr(validation.threading, "Thread", _SynchronousThread)
    monkeypatch.setattr(
        validation, "_http_post_json", lambda *a, **k: ({"is_supported": False}, None)
    )
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    failing_path = validation.result_path(controls) if failing == "result" else controls
    real_write = validation.atomic_write_json

    def write(path, value):
        if path == failing_path:
            raise OSError("read-only")
        real_write(path, value)

    monkeypatch.setattr(validation, "atomic_write_json", write)
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    if failing == "result":
        assert not validation.result_path(controls).exists()
        assert session_routing_enabled(controls) is False
    else:
        assert json.loads(validation.result_path(controls).read_text()) == {
            "routing_disabled": False
        }
        assert controls.read_text() == "{}"


def test_unsupported_result_carries_the_details_for_the_logs_row(tmp_path, monkeypatch):
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
    monkeypatch.setattr(validation, "_http_post_json", lambda *a, **k: (payload, None))
    controls = tmp_path / "env.json"
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    result = _wait_for_result(validation.result_path(controls))
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
        "available_route_options": [{"model": "claude-sonnet-5-5", "harness": "claude_code"}],
    }
