"""Client telemetry contracts, using the real OTel SDK and protobuf encoder."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.sdk.metrics.export import AggregationTemporality

from ucode import databricks, metrics, metrics_export, workspace

HOST = "https://dbc-test.cloud.databricks.com"


def observations(metrics_recorder, name):
    return [
        (value, labels) for _, metric, value, labels in metrics_recorder.pending if metric == name
    ]


def test_bounds_buffer_and_drops_oldest(metrics_recorder):
    for n in range(1500):
        metrics.record("http_request_duration_ms", n, api_operation="models_list")
    assert len(metrics_recorder.pending) == 1000
    assert metrics_recorder.pending[0][2] == 500
    assert metrics_recorder.pending[-1][2] == 1499


def test_concurrent_observations_keep_individual_labels(metrics_recorder):
    def emit(i):
        with metrics.measure("databricks_token_fetch_duration_ms", token_source="cli"):
            metrics.annotate(in_process_cache_hit=bool(i % 2))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(emit, range(50)))
    labels = [
        attrs for _, attrs in observations(metrics_recorder, "databricks_token_fetch_duration_ms")
    ]
    assert len(labels) == 50
    assert sum(attrs["in_process_cache_hit"] for attrs in labels) == 25


def test_workspace_handoffs_do_not_mix_tokens_or_selected_ids(metrics_recorder, monkeypatch):
    shared = "https://shared.cloud.databricks.com"
    workspace.activate(shared, "111")
    metrics.bind(shared, "first-token")
    with metrics.measure("ug_usage_duration_ms", workspace=shared):
        workspace.activate(shared, "222")
        metrics.bind(shared, "second-token")
    metrics.record("ug_usage_duration_ms", 4)
    try:
        rows = list(metrics_recorder.pending)
        assert rows[0][0] == f"{shared}?w=111"
        assert rows[1][0] == f"{shared}?w=222"
        assert metrics_recorder.credentials[f"{shared}?w=111"] == (
            "first-token",
            {"X-Databricks-Org-Id": "111", "X-Databricks-Workspace-Id": "111"},
        )
        assert metrics_recorder.credentials[f"{shared}?w=222"][0] == "second-token"
    finally:
        workspace.activate(None, None)


def test_no_credentials_means_no_upload_or_login(monkeypatch):
    from ucode.os_compatibility import subprocess_cross_os

    spawn = Mock()
    login = Mock(side_effect=AssertionError("telemetry must not acquire credentials"))
    monkeypatch.setattr(subprocess_cross_os, "popen", spawn)
    monkeypatch.setattr(databricks, "get_databricks_token", login)
    assert (
        metrics_export.send_in_background([(None, "dependency_setup_duration_ms", 12, {})], {})
        is None
    )
    spawn.assert_not_called()
    login.assert_not_called()


def test_startup_includes_login_and_stops_once_at_handoff(metrics_recorder, monkeypatch):
    ticks = iter([12.0, 12.0])
    metrics_recorder.started = 2.0
    monkeypatch.setattr(metrics.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(metrics_recorder, "start_periodic", lambda: None)
    metrics.start_agent("claude", preflight_skipped=True, first_time_setup=True)
    metrics.mark_login()
    captured = []
    monkeypatch.setattr(
        metrics_recorder, "flush", lambda: captured.extend(list(metrics_recorder.pending))
    )
    metrics.agent_handoff()
    metrics.agent_handoff()
    startup = [row for row in captured if row[1] == "ug_startup_duration_ms"]
    assert len(startup) == 1
    assert startup[0][2] == 10000
    assert startup[0][3]["interactive_login"] is True
    assert startup[0][3]["preflight_skipped"] is True
    assert metrics_recorder.phase == "agent_session"


def test_failed_startup_is_flushed_with_its_duration(metrics_recorder, monkeypatch):
    metrics.start_agent("codex")
    metrics.failure("databricks_token_fetch", RuntimeError("credential failed"))
    captured = []
    monkeypatch.setattr(
        metrics_recorder, "flush", lambda: captured.extend(list(metrics_recorder.pending))
    )
    metrics.finish()
    assert sum(row[1] == "ug_startup_duration_ms" for row in captured) == 1
    errors = [row[3] for row in captured if row[1] == "ug_cli_error"]
    assert errors[0]["phase"] == "startup"
    assert errors[0]["operation"] == "databricks_token_fetch"


def test_periodic_export_uses_60_seconds_and_stops(metrics_recorder, monkeypatch):
    flushed = threading.Event()
    monkeypatch.setattr(metrics_recorder, "flush", flushed.set)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        return len(waits) > 1

    monkeypatch.setattr(metrics_recorder.stop, "wait", wait)
    metrics_recorder.start_periodic()
    assert flushed.wait(2)
    metrics_recorder.thread.join(timeout=2)
    assert waits == [60, 60]


def test_busy_reporter_keeps_queue_bounded_without_more_children(metrics_recorder):
    metrics_recorder.reporter = Mock()
    metrics_recorder.reporter.poll.return_value = None
    for n in range(1100):
        metrics.record("proxy_response_bytes", n)
    metrics.flush()
    metrics_export.send_in_background.assert_not_called()
    assert len(metrics_recorder.pending) == 1000


@pytest.mark.parametrize("busy", ["reporter", "export_lock"])
def test_shutdown_drops_tail_without_waiting_for_busy_export(metrics_recorder, busy):
    metrics.record("ug_usage_duration_ms", 12)
    if busy == "reporter":
        metrics_recorder.reporter = Mock()
        metrics_recorder.reporter.poll.return_value = None
    else:
        metrics_recorder.export_lock.acquire()
    finished = threading.Event()

    def shutdown():
        metrics.finish()
        finished.set()

    worker = threading.Thread(target=shutdown, daemon=True)
    worker.start()
    try:
        assert finished.wait(2), "shutdown waited for the busy exporter"
        assert metrics._runtime is None
        assert metrics_recorder.stop.is_set()
        metrics_export.send_in_background.assert_not_called()
        if busy == "reporter":
            metrics_recorder.reporter.wait.assert_not_called()
    finally:
        if busy == "export_lock":
            metrics_recorder.export_lock.release()
        worker.join(timeout=2)


def test_shutdown_failure_preserves_command_exception(metrics_recorder, monkeypatch):
    metrics.start_agent("codex")
    original = RuntimeError("command failed")
    # Fault the clock and upload boundaries, not the cleanup method under test.
    monkeypatch.setattr(metrics.time, "perf_counter", Mock(side_effect=OSError("clock failed")))
    monkeypatch.setattr(
        metrics_export, "send_in_background", Mock(side_effect=OSError("spawn failed"))
    )

    with pytest.raises(RuntimeError) as raised:
        try:
            raise original
        finally:
            metrics.finish()

    assert raised.value is original
    assert metrics._runtime is None
    assert metrics_recorder.stop.is_set()


def test_export_failure_does_not_change_command_result(metrics_recorder, monkeypatch):
    metrics.bind(HOST, "not-a-real-token")
    metrics.record("ug_usage_duration_ms", 10)
    monkeypatch.setattr(metrics_export, "send_in_background", Mock(side_effect=OSError("full")))
    metrics.flush()
    assert not metrics_recorder.pending


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("connection reset at /oauth/token"), "network_error"),
        ("network unavailable during login", "network_error"),
        (TimeoutError("/oauth/token timed out"), "timeout"),
        (PermissionError("token file"), "permission_denied"),
        (RuntimeError("HTTP 401 while fetching token"), "http_401"),
        (RuntimeError("HTTP 503 during login"), "http_503"),
        (RuntimeError("invalid token"), "authentication_failed"),
    ],
)
def test_error_classification_preserves_failure_cause(metrics_recorder, error, expected):
    metrics.failure("databricks_token_fetch", error)
    assert observations(metrics_recorder, "ug_cli_error")[0][1]["error_code"] == expected


def test_error_reporting_failure_preserves_original_exception(metrics_recorder):
    class UnprintableError(RuntimeError):
        def __str__(self):
            raise ValueError("error formatting failed")

    original = UnprintableError()
    calls = []

    @metrics.timed("ug_usage_duration_ms")
    def command():
        calls.append("called")
        raise original

    with pytest.raises(UnprintableError) as raised:
        command()
    assert raised.value is original
    assert calls == ["called"]


def test_error_reporting_failure_preserves_returned_result(metrics_recorder):
    class UnprintableReason(str):
        def __str__(self):
            raise ValueError("error formatting failed")

    result = ([], UnprintableReason("fetch failed"))
    calls = []

    @metrics.timed("coding_agent_config_fetch_duration_ms", reason_result=True)
    def fetch():
        calls.append("called")
        return result

    assert fetch() is result
    assert calls == ["called"]


def test_error_reporting_survives_concurrent_shutdown(metrics_recorder):
    entered = threading.Event()
    resume = threading.Event()
    failures = []

    class PausedReason(str):
        def __str__(self):
            entered.set()
            assert resume.wait(2), "shutdown did not release error reporting"
            return super().__str__()

    def report():
        try:
            with metrics.measure("ug_usage_duration_ms"):
                metrics.failure("ug_usage", PausedReason("fetch failed"))
        except Exception as error:
            failures.append(error)

    worker = threading.Thread(target=report, daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        metrics.finish()
    finally:
        resume.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert not failures
    assert metrics._runtime is None


def test_final_error_is_not_duplicated_when_wrapped(metrics_recorder):
    @metrics.timed("databricks_token_fetch_duration_ms")
    def fetch():
        raise TimeoutError("https://private.example/secret?token=secret")

    try:
        fetch()
    except TimeoutError as exc:
        try:
            raise RuntimeError("wrapped secret") from exc
        except RuntimeError as outer:
            metrics.failure("command", outer)
    errors = observations(metrics_recorder, "ug_cli_error")
    assert len(errors) == 1
    assert errors[0][1]["error_code"] == "timeout"
    assert "secret" not in json.dumps(list(metrics_recorder.pending))


@pytest.mark.parametrize("phase", ["startup", "command", "agent_session"])
def test_errors_cover_every_phase_with_recovery_labels(metrics_recorder, phase):
    metrics_recorder.phase = phase
    metrics.failure("skills_refresh", "HTTP 503 private response", continued_with_fallback=True)
    value, attrs = observations(metrics_recorder, "ug_cli_error")[0]
    assert value == 1
    assert attrs["phase"] == phase
    assert attrs["error_code"] == "http_503"
    assert attrs["continued_with_fallback"] is True


def test_recovered_function_records_returned_error(metrics_recorder):
    @metrics.timed(
        "coding_agent_config_fetch_duration_ms", reason_result=True, continued_with_fallback=True
    )
    def load():
        return [], "HTTP 403 details that must not be logged"

    assert load()[0] == []
    value, attrs = observations(metrics_recorder, "ug_cli_error")[0]
    assert value == 1
    assert attrs["continued_with_fallback"]


def metric_data(data):
    return {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


def test_sdk_aggregates_histograms_and_delta_counters_without_empty_metrics():
    result = metrics_export.aggregate(
        [
            ("ug_usage_duration_ms", 10, {"command": "usage", "show_all": False}),
            ("ug_usage_duration_ms", 30, {"command": "usage", "show_all": False}),
            (
                "ug_cli_error",
                1,
                {"phase": "command", "operation": "ug_usage", "error_code": "timeout"},
            ),
            (
                "ug_cli_error",
                1,
                {"phase": "command", "operation": "ug_usage", "error_code": "timeout"},
            ),
        ]
    )
    data = metric_data(result)
    assert set(data) == {"ug_usage_duration_ms", "ug_cli_error"}
    hist = data["ug_usage_duration_ms"].data
    assert hist.aggregation_temporality == AggregationTemporality.DELTA
    point = hist.data_points[0]
    assert (point.count, point.sum, point.min, point.max) == (2, 40, 10, 30)
    assert sum(point.bucket_counts) == 2
    assert len(point.bucket_counts) == len(point.explicit_bounds) + 1
    assert data["ug_cli_error"].data.data_points[0].value == 2
    assert data["ug_cli_error"].data.is_monotonic
    resource = result.resource_metrics[0].resource.attributes
    assert {"ug_version", "os", "arch"} <= set(resource)


def test_sdk_preserves_individual_labels_and_excludes_unknown_attributes():
    result = metrics_export.aggregate(
        [
            ("ug_usage_duration_ms", 10, {"show_all": False, "prompt": "private"}),
            ("ug_usage_duration_ms", 30, {"show_all": True, "api_operation": "https://private"}),
        ]
    )
    points = metric_data(result)["ug_usage_duration_ms"].data.data_points
    assert {p.attributes["show_all"] for p in points} == {False, True}
    assert all(set(p.attributes) == {"show_all"} for p in points)


def test_standard_exporter_wire_format_and_workspace_headers(monkeypatch):
    import requests

    sent = []

    def post(_session, url, **kwargs):
        sent.append((url, kwargs, dict(_session.headers)))
        response = requests.Response()
        response.status_code = 200
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "post", post)
    metrics_export.export_batch(
        {
            "workspace": HOST + "?w=111",
            "token": "fake-token",
            "headers": {"X-Databricks-Workspace-Id": "111"},
            "observations": [("ug_usage_duration_ms", 10, {"show_all": False})],
        }
    )
    assert len(sent) == 1
    url, kwargs, headers = sent[0]
    assert headers["Content-Type"] == "application/x-protobuf"
    assert headers["Authorization"] == "Bearer fake-token"
    assert headers["X-Databricks-Workspace-Id"] == "111"
    assert url == HOST + "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"
    assert kwargs["allow_redirects"] is False
    request = ExportMetricsServiceRequest.FromString(kwargs["data"])
    metric = request.resource_metrics[0].scope_metrics[0].metrics[0]
    assert metric.name == "ug_usage_duration_ms"
    assert metric.histogram.aggregation_temporality == 1  # OTLP DELTA
    assert metric.histogram.data_points[0].count == 1


def test_exporter_stops_after_disabled_endpoint(monkeypatch):
    import requests

    calls = []

    def post(_session, url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 404
        response._content = b""
        return response

    monkeypatch.setattr(requests.Session, "post", post)
    metrics_export.export_batch(
        {
            "workspace": HOST,
            "token": "fake",
            "headers": {},
            "observations": [("ug_usage_duration_ms", n, {}) for n in range(250)],
        }
    )
    assert len(calls) == 1


def test_recording_does_not_import_sdk_or_http_client(monkeypatch):
    monkeypatch.setenv("UG_ENABLE_METRICS_EXPORT", "1")
    code = "from ucode import metrics; metrics.begin('usage'); metrics.record('ug_usage_duration_ms', 1); assert len(metrics._runtime.pending) == 1; import sys; print('opentelemetry.sdk' in sys.modules); print('requests' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "False\nFalse\n"


def test_subagent_trigger_is_scoped(metrics_recorder):
    assert metrics.routing_trigger() == "first_prompt"
    with metrics.subagent_routing():
        assert metrics.routing_trigger() == "subagent"
    assert metrics.routing_trigger() == "first_prompt"


def test_detached_handoff_keeps_token_off_argv_and_does_not_wait(monkeypatch):
    import io

    from ucode.os_compatibility import subprocess_cross_os

    class Pipe(io.BytesIO):
        def close(self):
            self.handoff = self.getvalue()
            super().close()

    child = Mock(stdin=Pipe())
    spawn = Mock(return_value=child)
    monkeypatch.setattr(subprocess_cross_os, "popen", spawn)
    returned = metrics_export.send_in_background(
        [(HOST, "ug_usage_duration_ms", 10, {"command": "usage"})],
        {HOST: ("fake-workspace-token", {"X-Databricks-Workspace-Id": "111"})},
    )
    assert returned is child
    child.wait.assert_not_called()
    child.communicate.assert_not_called()
    args, kwargs = spawn.call_args
    assert "fake-workspace-token" not in str(args)
    assert "env" not in kwargs
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs.get("start_new_session") or kwargs.get("creationflags")
    assert json.loads(child.stdin.handoff)[0] == {
        "workspace": HOST,
        "token": "fake-workspace-token",
        "headers": {"X-Databricks-Workspace-Id": "111"},
        "observations": [["ug_usage_duration_ms", 10, {"command": "usage"}]],
    }


@pytest.mark.parametrize("value", [None, "0", "false", "invalid"])
def test_export_disabled_creates_no_buffer_thread_or_uploader(monkeypatch, value):
    monkeypatch.setattr(metrics, "_runtime", None)
    if value is None:
        monkeypatch.delenv("UG_ENABLE_METRICS_EXPORT", raising=False)
    else:
        monkeypatch.setenv("UG_ENABLE_METRICS_EXPORT", value)
    thread = Mock()
    upload = Mock()
    monkeypatch.setattr(metrics.threading, "Thread", thread)
    monkeypatch.setattr(metrics_export, "send_in_background", upload)

    metrics.begin("claude")
    metrics.start_agent("claude")
    metrics.bind(HOST, "fake-token")
    metrics.record("ug_startup_duration_ms", 1)
    metrics.start_periodic()
    metrics.agent_handoff()
    metrics.finish()

    assert metrics._runtime is None
    thread.assert_not_called()
    upload.assert_not_called()


@pytest.mark.parametrize("value", ["1", "true"])
def test_export_enabled_collects_and_hands_off_metrics(monkeypatch, value):
    monkeypatch.setattr(metrics, "_runtime", None)
    monkeypatch.setenv("UG_ENABLE_METRICS_EXPORT", value)
    upload = Mock(return_value=None)
    monkeypatch.setattr(metrics_export, "send_in_background", upload)

    try:
        metrics.begin("usage")
        metrics.bind(HOST, "fake-token")
        metrics.record("ug_usage_duration_ms", 12)
        metrics.flush()
        upload.assert_called_once_with(
            [(HOST, "ug_usage_duration_ms", 12, {"command": "usage"})],
            {HOST: ("fake-token", {})},
        )
        assert not metrics._runtime.pending
    finally:
        metrics.finish()


def test_collection_uses_ug_flag_independently_of_global_otel_setting(monkeypatch):
    monkeypatch.setattr(metrics, "_runtime", None)
    monkeypatch.setenv("UG_ENABLE_METRICS_EXPORT", "1")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    upload = Mock(return_value=None)
    monkeypatch.setattr(metrics_export, "send_in_background", upload)
    try:
        metrics.begin("usage")
        metrics.record("ug_usage_duration_ms", 12)
        assert len(metrics._runtime.pending) == 1
    finally:
        metrics.finish()


def test_interactive_dependency_prompt_is_visible_through_nested_installer(metrics_recorder):
    with metrics.measure("dependency_setup_duration_ms", interactive_wait=False):
        with metrics.measure("dependency_install_duration_ms", dependency="codex"):
            metrics.mark_interactive_wait()
    assert observations(metrics_recorder, "dependency_setup_duration_ms")[0][1]["interactive_wait"]
