"""Detached, bounded metric buffer; flush on a timer, agent handoff, or parent exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import defaultdict, deque
from contextlib import suppress
from typing import TYPE_CHECKING, Any, BinaryIO
from urllib.parse import urlsplit, urlunsplit

from ucode.metrics import EXPORT_INTERVAL_SECONDS, MAX_HANDOFF_BYTES, MAX_OBSERVATIONS

if TYPE_CHECKING:
    from opentelemetry.sdk.metrics.export import MetricsData

INGEST_PATH = "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"
EXPORT_TIMEOUT_SECONDS = 5
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


def start_exporter():
    from ucode.os_compatibility import subprocess_cross_os

    options: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    reporter = subprocess_cross_os.popen(
        [sys.executable, "-P", "-m", "ucode.metrics.exporter"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
        bufsize=0,
        **options,
    )
    assert reporter.stdin is not None
    try:
        # Unbuffered, nonblocking writes cannot hold up a command, even if the child stalls.
        os.set_blocking(reporter.stdin.fileno(), False)
    except Exception:
        reporter.stdin.close()
        raise
    return reporter


class ExportBuffer:
    def __init__(self):
        self.pending: deque = deque(maxlen=MAX_OBSERVATIONS)
        self.credentials: dict = {}
        self.workspace: str | None = None
        self.context: dict = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = threading.Event()

    def receive(self, message: dict) -> None:
        with self.lock:
            if "credentials" in message:
                self.credentials = message["credentials"]
            if self.workspace is None and message.get("workspace"):
                self.workspace = message["workspace"]
                self.pending = deque(
                    ((w or self.workspace, n, v, a) for w, n, v, a in self.pending),
                    maxlen=MAX_OBSERVATIONS,
                )
            context = message.get("context", {})
            if context != self.context:
                for _, _, _, attrs in self.pending:
                    if "command" in context:
                        attrs["command"] = context["command"]
                    if "agent" in context and attrs.get("agent") in (None, "unknown"):
                        attrs["agent"] = context["agent"]
                self.context = context
            for workspace, name, value, labels in message.get("observations", []):
                self.pending.append((workspace or self.workspace, name, value, labels))
        if message.get("flush"):
            self.wake.set()

    def read(self, stream: BinaryIO) -> None:
        try:
            while line := stream.readline(MAX_HANDOFF_BYTES + 1):
                if len(line) > MAX_HANDOFF_BYTES or not line.endswith(b"\n"):
                    break
                self.receive(json.loads(line))
        except Exception:
            return
        finally:
            self.closed.set()
            self.wake.set()

    def drain(self) -> list[dict]:
        grouped: dict[str, list] = defaultdict(list)
        with self.lock:
            unbound: deque = deque(maxlen=MAX_OBSERVATIONS)
            for workspace, name, value, labels in self.pending:
                if workspace in self.credentials:
                    grouped[workspace].append((name, value, labels))
                else:
                    unbound.append((workspace, name, value, labels))
            self.pending = unbound
            return [
                {
                    "workspace": workspace,
                    "token": self.credentials[workspace][0],
                    "headers": self.credentials[workspace][1],
                    "observations": points,
                }
                for workspace, points in grouped.items()
            ]

    def run(self, stream: BinaryIO) -> None:
        # Keep draining the pipe while SDK initialization or an HTTP upload is in progress.
        threading.Thread(target=self.read, args=(stream,), daemon=True).start()
        while True:
            self.wake.wait(EXPORT_INTERVAL_SECONDS)
            self.wake.clear()
            closed = self.closed.is_set()
            for batch in self.drain():
                with suppress(Exception):
                    export_batch(batch)
            if closed:
                return


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
    with suppress(Exception):
        ExportBuffer().run(sys.stdin.buffer)


if __name__ == "__main__":
    main()
