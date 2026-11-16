"""Assert the OTLP wire contract captured from real installed CLI processes."""

from __future__ import annotations

import gzip
import math
import time

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import AGGREGATION_TEMPORALITY_DELTA

from .request_recorder import TuiRequestRecorder

METRICS_PATH = "/api/2.0/ai-gateway/ug-cli/otel/v1/metrics"


def _attributes(attributes):
    return {item.key: getattr(item.value, item.value.WhichOneof("value")) for item in attributes}


def _decode_body(message):
    encoding = message.headers.get("content-encoding", "identity").lower()
    assert encoding in {"identity", "gzip"}, f"Unexpected OTLP encoding: {encoding}"
    return gzip.decompress(message.body) if encoding == "gzip" else message.body


def assert_metrics_delivered(
    recorder: TuiRequestRecorder,
    expected: dict[str, dict],
    *,
    after: int = 0,
    timeout: float = 90,
) -> list[dict]:
    """Require matching histograms and gateway acknowledgments, across upload batches."""
    missing = set(expected)
    uploads = []
    deadline = time.monotonic() + timeout
    while missing:
        try:
            request = recorder.expect_request(
                method="POST",
                path=METRICS_PATH,
                after=after,
                timeout=max(0, deadline - time.monotonic()),
            )
        except AssertionError as error:
            raise AssertionError(
                f"Metrics not delivered: {sorted(missing)}; acknowledged uploads: {uploads}"
            ) from error
        after = request.sequence
        assert request.headers.get("content-type", "").split(";")[0] == "application/x-protobuf"
        assert request.headers.get("authorization") == "<redacted>"
        response = recorder.response_for(request, timeout=max(0, deadline - time.monotonic()))
        assert response.status_code == 200, (
            f"OTLP upload {request.sequence} returned HTTP {response.status_code}; "
            "the selected workspace must enable UG CLI metrics ingestion"
        )
        assert response.headers.get("content-type", "").split(";")[0] == "application/x-protobuf"
        ack = ExportMetricsServiceResponse.FromString(_decode_body(response))
        assert ack.partial_success.rejected_data_points == 0, "Gateway rejected metric data points"
        payload = ExportMetricsServiceRequest.FromString(_decode_body(request))
        assert payload.resource_metrics, "OTLP upload contained no resource metrics"
        matched = []
        names = []
        for resource in payload.resource_metrics:
            attrs = _attributes(resource.resource.attributes)
            assert all(attrs.get(key) for key in ("ug_version", "os", "arch")), attrs
            for scope in resource.scope_metrics:
                assert scope.scope.name == "unity-gateway.cli", scope.scope.name
                for metric in scope.metrics:
                    names.append(metric.name)
                    if metric.name not in expected:
                        continue
                    assert metric.WhichOneof("data") == "histogram", metric.name
                    assert metric.unit == ("By" if metric.name == "proxy_response_bytes" else "ms")
                    histogram = metric.histogram
                    assert histogram.aggregation_temporality == AGGREGATION_TEMPORALITY_DELTA
                    for point in histogram.data_points:
                        labels = _attributes(point.attributes)
                        if not expected[metric.name].items() <= labels.items():
                            continue
                        assert point.count > 0, metric.name
                        assert point.HasField("sum") and math.isfinite(point.sum), metric.name
                        assert point.sum >= 0, metric.name
                        assert sum(point.bucket_counts) == point.count, metric.name
                        assert len(point.bucket_counts) == len(point.explicit_bounds) + 1
                        assert 0 < point.start_time_unix_nano <= point.time_unix_nano
                        if metric.name == "proxy_response_bytes":
                            assert point.sum > 0, "Successful inference had no response bytes"
                        missing.discard(metric.name)
                        matched.append(metric.name)
        uploads.append(
            {
                "sequence": request.sequence,
                "status": response.status_code,
                "metrics": sorted(set(names)),
                "matched": sorted(set(matched)),
            }
        )
    return uploads
