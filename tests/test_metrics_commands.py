"""Usage and routing emit durations on both success and failure."""

from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from ucode import cli, metrics
from ucode.smart_routing import routing


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
    assert labels["http_request_duration_ms"]["api_operation"] == "routes_select"
    assert labels["http_request_duration_ms"]["http_status_code"] == 503
    assert labels["ug_cli_error"]["continued_with_fallback"] is True
    assert "PRIVATE PROMPT" not in str(rows)
