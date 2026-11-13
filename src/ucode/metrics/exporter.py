"""Detached OTel SDK/exporter. Receive the complete handoff before importing the SDK."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import MetricsData

INGEST_PATH = "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"
EXPORT_TIMEOUT_SECONDS = 5
MAX_HANDOFF_BYTES = 2 * 1024 * 1024
MAX_POINTS_PER_REQUEST = 100
DURATION_BOUNDS = (
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    2500,
    5000,
    10000,
    30000,
    60000,
    120000,
    300000,
    600000,
    3600000,
)
BYTE_BOUNDS = (256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304, 16777216)


def send_in_background(observations: list, credentials: dict):
    from ucode.os_compatibility import subprocess_cross_os

    grouped: dict[str, list] = defaultdict(list)
    for workspace, name, value, labels in observations:
        if workspace in credentials:
            grouped[workspace].append((name, value, labels))
    if not grouped:
        return None
    batches = [
        {
            "workspace": workspace,
            "token": credentials[workspace][0],
            "headers": credentials[workspace][1],
            "observations": points,
        }
        for workspace, points in grouped.items()
    ]
    payload = json.dumps(batches, separators=(",", ":")).encode()
    if len(payload) > MAX_HANDOFF_BYTES:
        return None
    options: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with suppress(OSError):
        reporter = subprocess_cross_os.popen(
            [sys.executable, "-P", "-m", "ucode.metrics.exporter"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        assert reporter.stdin is not None
        # The child drains stdin before SDK initialization or networking. Credentials never go
        # into argv, an added environment variable, or a persisted spool file.
        with reporter.stdin:
            reporter.stdin.write(payload)
        return reporter
    return None


def aggregate(observations: list) -> MetricsData | None:
    import platform

    from opentelemetry.metrics import Counter as ApiCounter
    from opentelemetry.metrics import Histogram as ApiHistogram
    from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource

    from ucode.metrics import COUNTER_METRICS, METRICS, _labels
    from ucode.telemetry import normalize_arch, normalize_os, ug_version

    reader = InMemoryMetricReader(
        preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        }
    )
    provider = MeterProvider(
        metric_readers=[reader],
        shutdown_on_exit=False,
        resource=Resource(
            {
                "ug_version": ug_version(),
                "os": normalize_os(sys.platform),
                "arch": normalize_arch(platform.machine()),
            }
        ),
        views=[
            View(
                instrument_type=Histogram,
                instrument_name="*_duration_ms",
                aggregation=ExplicitBucketHistogramAggregation(DURATION_BOUNDS),
            ),
            View(
                instrument_name="proxy_response_bytes",
                aggregation=ExplicitBucketHistogramAggregation(BYTE_BOUNDS),
            ),
        ],
    )
    try:
        meter = provider.get_meter("unity-gateway.cli")
        instruments: dict[str, ApiCounter | ApiHistogram] = {}
        for name, value, labels in observations:
            if name not in METRICS:
                continue
            if name not in instruments:
                instruments[name] = (
                    meter.create_counter(name, unit="1")
                    if name in COUNTER_METRICS
                    else meter.create_histogram(
                        name, unit="By" if name == "proxy_response_bytes" else "ms"
                    )
                )
            instrument = instruments[name]
            if isinstance(instrument, ApiCounter):
                instrument.add(int(value), _labels(labels))
            else:
                instrument.record(value, _labels(labels))
        return reader.get_metrics_data()
    finally:
        provider.shutdown()


def export_batch(batch: dict) -> None:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import Counter, Histogram
    from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExportResult
    from requests import PreparedRequest, Session
    from requests.auth import AuthBase

    parsed = urlsplit(batch["workspace"])
    if parsed.scheme != "https" or not parsed.netloc:
        return
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, INGEST_PATH, "", ""))

    class NoRedirectSession(Session):
        def post(self, url, *args, **kwargs):
            # A telemetry redirect must never forward a workspace bearer to another host.
            kwargs["allow_redirects"] = False
            return super().post(url, *args, **kwargs)

    class WorkspaceBearerAuth(AuthBase):
        def __call__(self, r: PreparedRequest) -> PreparedRequest:
            r.headers["Authorization"] = f"Bearer {batch['token']}"
            return r

    session = NoRedirectSession()
    # Explicit auth prevents Requests from replacing the workspace bearer via .netrc.
    session.auth = WorkspaceBearerAuth()
    exporter = OTLPMetricExporter(
        endpoint=endpoint,
        timeout=EXPORT_TIMEOUT_SECONDS,
        session=session,
        headers=batch["headers"],
        preferred_temporality={
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        },
    )
    try:
        observations = batch["observations"]
        for start in range(0, len(observations), MAX_POINTS_PER_REQUEST):
            data = aggregate(observations[start : start + MAX_POINTS_PER_REQUEST])
            if data is not None and exporter.export(data) != MetricExportResult.SUCCESS:
                break
    finally:
        exporter.shutdown()


def main() -> None:
    # No SDK import can delay the parent's pipe handoff.
    payload = sys.stdin.buffer.read(MAX_HANDOFF_BYTES + 1)
    if len(payload) > MAX_HANDOFF_BYTES:
        return
    try:
        for batch in json.loads(payload):
            export_batch(batch)
    except Exception:
        # Best-effort telemetry never writes to the user's terminal or changes command status.
        return


if __name__ == "__main__":
    main()
