"""Usage and routing emit durations on both success and failure."""

import threading
import time
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from ucode import cli, metrics
from ucode.smart_routing import codex_routing, routing, v2


def test_usage_duration_includes_output_and_preserves_all_label(metrics_recorder, monkeypatch):
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)
    output = Mock()
    monkeypatch.setattr(cli, "usage_report", output)
    cli.usage_cmd(show_all=True)
    output.assert_called_once_with(show_all=True)
    row = next(a for _, n, _, a in metrics_recorder.pending if n == "ug_usage_duration_ms")
    assert row["show_all"] is True


def test_usage_failure_is_counted_once(metrics_recorder, monkeypatch):
    monkeypatch.setattr(cli, "install_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "usage_report", Mock(side_effect=RuntimeError("HTTP 403 private")))
    with pytest.raises(cli.typer.Exit):
        cli.usage_cmd(show_all=False)
    errors = [a for _, n, _, a in metrics_recorder.pending if n == "ug_cli_error"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == "http_403"
    assert any(n == "ug_usage_duration_ms" for _, n, _, _ in metrics_recorder.pending)


def test_subagent_routing_http_failure_records_fallback_without_prompt(
    metrics_recorder, monkeypatch
):
    monkeypatch.setattr(
        routing.urllib.request,
        "urlopen",
        Mock(side_effect=HTTPError("https://example.databricks.com", 503, "unavailable", {}, None)),
    )
    with metrics.subagent_routing():
        decision, reason = routing.select_route(
            "https://example.databricks.com",
            "fake-token",
            "PRIVATE PROMPT",
            [("model", "claude")],
            router_name="stable",
        )
    assert decision is None and "503" in reason
    rows = list(metrics_recorder.pending)
    labels = {n: a for _, n, _, a in rows}
    assert labels["routing_decision_duration_ms"]["routing_trigger"] == "subagent"
    assert labels["routing_decision_duration_ms"]["http_status_code"] == 503
    assert labels["http_request_duration_ms"]["api_operation"] == "routes_select"
    assert labels["http_request_duration_ms"]["http_status_code"] == 503
    assert labels["ug_cli_error"]["continued_with_fallback"] is True
    assert "PRIVATE PROMPT" not in str(rows)


def _select(route_options=(("model", "claude"),), **kwargs):
    return routing.select_route(
        "https://example.databricks.com",
        "fake-token",
        "PRIVATE PROMPT",
        list(route_options),
        router_name="stable",
        **kwargs,
    )


def test_route_selection_defaults_to_ten_second_deadline():
    assert routing.ROUTE_SELECTION_TIMEOUT_S == 10.0
    assert routing.REQUEST_TIMEOUT_S == routing.ROUTE_SELECTION_TIMEOUT_S
    assert codex_routing.REQUEST_TIMEOUT_S == routing.ROUTE_SELECTION_TIMEOUT_S
    assert v2.CLAUDE_ROUTE_SELECTION_TIMEOUT_S == routing.ROUTE_SELECTION_TIMEOUT_S


def test_route_selection_timeout_falls_back_and_records_metrics(metrics_recorder, monkeypatch):
    release = threading.Event()

    def stalled_router(*_args, **_kwargs):
        release.wait(5)
        raise OSError("released")

    monkeypatch.setattr(routing.urllib.request, "urlopen", stalled_router)
    started = time.monotonic()
    try:
        decision, reason = _select(timeout=0.2)
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert decision is None and "timed out" in reason
    errors = [a for _, n, _, a in metrics_recorder.pending if n == "ug_cli_error"]
    assert len(errors) == 1
    assert errors[0]["operation"] == "routing_decision"
    assert errors[0]["error_code"] == "timeout"
    assert errors[0]["error_type"] == "RouteSelectionTimeout"
    assert errors[0]["agent"] == "claude"
    assert errors[0]["fallback_model"] == routing.HARNESS_DEFAULT_FALLBACK
    assert errors[0]["continued_with_fallback"] is True
    duration = next(
        a for _, n, _, a in metrics_recorder.pending if n == "routing_decision_duration_ms"
    )
    assert duration["error_type"] == "RouteSelectionTimeout"
    assert duration["fallback_model"] == routing.HARNESS_DEFAULT_FALLBACK


@pytest.mark.parametrize("status", [429, 503])
def test_route_selection_http_error_records_failure_code_and_harness_fallback(
    metrics_recorder, monkeypatch, status
):
    monkeypatch.setattr(
        routing.urllib.request,
        "urlopen",
        Mock(side_effect=HTTPError("https://example.databricks.com", status, "no", {}, None)),
    )
    with routing.fallback_model("gpt-start"):
        decision, reason = _select([("system.ai.gpt-5-6-sol", "codex")])
    assert decision is None and str(status) in reason
    errors = [a for _, n, _, a in metrics_recorder.pending if n == "ug_cli_error"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == f"http_{status}"
    assert errors[0]["http_status_code"] == status
    assert errors[0]["error_type"] == "HTTPError"
    assert errors[0]["agent"] == "codex"
    assert errors[0]["fallback_model"] == "gpt-start"
