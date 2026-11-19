"""Startup validation with controlled HTTP and real background workers."""

from __future__ import annotations

import io
import json
import time
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conftest import metric_observations
from ucode import databricks, metrics
from ucode.smart_routing import startup_validation as validation
from ucode.smart_routing.session_env import session_routing_enabled
from ucode.workspace import activate


def _wait_for_result(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail("validation worker did not publish a result")


@pytest.mark.parametrize(
    "response",
    [
        ({"is_supported": True}, None),
        ({"is_supported": False}, None),
        (None, "HTTP 503"),
        (None, "HTTP 404 Not Found"),
        ({"is_supported": "false"}, None),
        RuntimeError("network down"),
    ],
)
def test_unsupported_menu_turns_routing_off_and_failures_are_silent(
    response, tmp_path, monkeypatch, metrics_recorder
):
    post = (
        Mock(side_effect=response)
        if isinstance(response, Exception)
        else Mock(return_value=response)
    )
    monkeypatch.setattr(validation, "_http_post_json", post)
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        "https://example.com",
        "test-token",
        [("custom.model", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    result = _wait_for_result(validation.result_path(controls))
    post.assert_called_once()
    unsupported = isinstance(response, tuple) and (response[0] or {}).get("is_supported") is False
    assert result == {"routing_disabled": unsupported}
    assert session_routing_enabled(controls) is not unsupported
    if not unsupported:
        assert controls.read_text() == "{}"
    failed = isinstance(response, Exception) or response[1] not in (None, "HTTP 404 Not Found")
    errors = metric_observations(metrics_recorder, metrics.UG_CLI_ERROR)
    assert [(labels.get("operation"), labels.get("phase")) for _, labels in errors] == (
        [(metrics.STARTUP_VALIDATION_OPERATION, "startup")] if failed else []
    )


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
    monkeypatch.setattr(databricks.urllib_request, "urlopen", urlopen)
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


@pytest.mark.parametrize("late", ["past deadline", "toggled during check"])
def test_late_unsupported_result_leaves_routing_alone(late, tmp_path, monkeypatch):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    toggled = '{"ENABLE_SMART_ROUTING_V2": "1"}'

    def post(*args, **kwargs):
        if late == "toggled during check":
            controls.write_text(toggled)
        return {"is_supported": False}, None

    monkeypatch.setattr(validation, "_http_post_json", post)
    if late == "past deadline":
        clock = iter([0.0, validation.VALIDATION_TIMEOUT_SECONDS + 1])
        monkeypatch.setattr(validation, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    assert _wait_for_result(validation.result_path(controls)) == {"routing_disabled": False}
    assert controls.read_text() == (toggled if late == "toggled during check" else "{}")


def test_result_write_failure_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validation.threading, "Thread", lambda *, target, daemon: SimpleNamespace(start=target)
    )
    writer = Mock(side_effect=RuntimeError("disk unavailable"))
    monkeypatch.setattr(validation, "atomic_write_json", writer)
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=tmp_path / "env.json",
    )
    writer.assert_called_once()


def test_empty_menu_skips_the_request(tmp_path, monkeypatch):
    post = Mock()
    monkeypatch.setattr(validation, "_http_post_json", post)
    controls = tmp_path / "env.json"
    validation.start(
        "https://example.com", "test-token", [], router_name="stable", session_controls=controls
    )
    assert json.loads(validation.result_path(controls).read_text()) == {"routing_disabled": False}
    post.assert_not_called()


def test_failed_controls_write_does_not_report_routing_off(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validation, "_http_post_json", lambda *a, **k: ({"is_supported": False}, None)
    )
    real_write = validation.atomic_write_json

    def write(path, value):
        if path == controls:
            raise OSError("read-only")
        real_write(path, value)

    monkeypatch.setattr(validation, "atomic_write_json", write)
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        "https://example.com",
        "test-token",
        [("m", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    assert _wait_for_result(validation.result_path(controls)) == {"routing_disabled": False}
