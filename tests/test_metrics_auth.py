"""Telemetry emitted by real UG authentication and HTTP helpers."""

from unittest.mock import Mock
from urllib.error import HTTPError

from ucode import databricks

HOST = "https://dbc-test.cloud.databricks.com"


def observations(recorder, name):
    return [(v, a) for _, n, v, a in recorder.pending if n == name]


def test_token_source_and_cache_hit(metrics_recorder, monkeypatch):
    monkeypatch.setenv("DATABRICKS_BEARER", "a-token")
    assert databricks.get_databricks_token(HOST) == "a-token"
    attrs = observations(metrics_recorder, "databricks_token_fetch_duration_ms")[0][1]
    assert attrs["token_source"] == "environment"
    assert attrs["in_process_cache_hit"] is False
    assert metrics_recorder.credentials[HOST][0] == "a-token"


def test_http_attempts_include_final_failure_but_retries_exclude_first(
    metrics_recorder, monkeypatch
):
    failures = [
        HTTPError(f"{HOST}/api/model-services", 503, "unavailable", {}, None),
        HTTPError(f"{HOST}/api/model-services", 403, "forbidden", {}, None),
    ]
    monkeypatch.setattr(databricks.urllib_request, "urlopen", Mock(side_effect=failures))
    monkeypatch.setattr(databricks.time, "sleep", lambda _: None)
    _, reason = databricks._http_get_bytes(f"{HOST}/api/model-services", "token", max_retries=3)
    assert "403" in reason
    attempts = observations(metrics_recorder, "http_request_duration_ms")
    assert [attrs["attempt"] for _, attrs in attempts] == [1, 2]
    assert [attrs["http_status_code"] for _, attrs in attempts] == [503, 403]
    assert sum(value for value, _ in observations(metrics_recorder, "http_retry_count")) == 1


def test_model_service_outer_retries_have_distinct_attempts_and_one_error(
    metrics_recorder, monkeypatch
):
    monkeypatch.setattr(
        databricks.urllib_request,
        "urlopen",
        Mock(side_effect=[HTTPError(HOST, 503, "unavailable", {}, None) for _ in range(3)]),
    )
    monkeypatch.setattr(databricks.time, "sleep", lambda _: None)
    _, reason = databricks._get_model_services_page(HOST + "/api/model-services", "fake", retries=3)
    assert "503" in reason
    attempts = observations(metrics_recorder, "http_request_duration_ms")
    assert [a["attempt"] for _, a in attempts] == [1, 2, 3]
    assert sum(v for v, _ in observations(metrics_recorder, "http_retry_count")) == 2
    assert len(observations(metrics_recorder, "ug_cli_error")) == 1
