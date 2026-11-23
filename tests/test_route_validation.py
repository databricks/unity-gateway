"""Startup validation with controlled HTTP and real background workers."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conftest import metric_observations
from ucode import metrics
from ucode.smart_routing import route_validation as validation
from ucode.smart_routing.session_env import session_routing_enabled


def _wait_for_result(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail("validation worker did not publish a result")


@pytest.mark.parametrize(
    "response,failed",
    [
        ((None, "HTTP 503"), True),
        (({"is_supported": "false"}, None), False),
        (RuntimeError("network down"), True),
    ],
)
def test_failed_or_malformed_check_leaves_routing_on(
    response, failed, tmp_path, monkeypatch, metrics_recorder
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
    assert result == {"routing_disabled": False}
    assert session_routing_enabled(controls) is True
    assert controls.read_text() == "{}"
    errors = metric_observations(metrics_recorder, metrics.UG_CLI_ERROR)
    assert [(labels["operation"], labels["continued_with_fallback"]) for _, labels in errors] == (
        [("route_validation", True)] if failed else []
    )
    assert len(metric_observations(metrics_recorder, metrics.ROUTE_VALIDATION_DURATION_MS)) == 1


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


def _validate(response, monkeypatch):
    monkeypatch.setattr(
        validation,
        "_http_post_json",
        Mock(side_effect=response)
        if isinstance(response, Exception)
        else Mock(return_value=response),
    )
    return validation.validate(
        "https://example.com", "test-token", [("m", "claude")], router_name="stable"
    )


@pytest.mark.parametrize("response", [(None, "HTTP 503"), RuntimeError("network down")])
def test_validate_returns_none_on_failure_so_the_toggle_stays_silent(response, monkeypatch):
    assert _validate(response, monkeypatch) is None
