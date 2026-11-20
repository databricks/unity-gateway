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
    "response,disabled,failed",
    [
        (({"is_supported": True}, None), False, False),
        (({"is_supported": False}, None), True, False),
        ((None, "HTTP 503"), False, True),
        (({"is_supported": "false"}, None), False, False),
        (RuntimeError("network down"), False, True),
    ],
)
def test_unsupported_model_list_turns_routing_off_and_failures_are_silent(
    response, disabled, failed, tmp_path, monkeypatch, metrics_recorder
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
    assert result == {"routing_disabled": disabled}
    assert session_routing_enabled(controls) is not disabled
    if not disabled:
        assert controls.read_text() == "{}"
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


def test_unsupported_result_past_the_deadline_leaves_routing_on(tmp_path, monkeypatch):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setattr(
        validation, "_http_post_json", lambda *a, **k: ({"is_supported": False}, None)
    )
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
    assert controls.read_text() == "{}"


@pytest.mark.parametrize("failing", ["result", "controls"])
def test_failed_write_is_isolated_and_keeps_the_result_honest(failing, tmp_path, monkeypatch):
    monkeypatch.setattr(
        validation.threading, "Thread", lambda *, target, daemon: SimpleNamespace(start=target)
    )
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
