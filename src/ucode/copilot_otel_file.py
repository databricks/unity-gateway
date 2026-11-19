"""Forwards the GitHub Copilot app's own spans from Copilot's file exporter to the gateway.

The app's runtime writes one JSON object per line to ``SPAN_FILE`` at the end of each turn, in
the OpenTelemetry JS SDK's in-memory shape rather than OTLP. ug's login service tails the file,
converts the spans to OTLP/JSON and posts them to the workspace's trace route.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from ucode.config_io import APP_DIR

SPAN_FILE = APP_DIR / "copilot-app" / "spans.jsonl"
# The gateway takes OTLP/JSON bodies up to 8 MiB; leave room for the conversion's growth.
MAX_BATCH_BYTES = 2 * 1024 * 1024
MAX_SEND_ATTEMPTS = 3
POLL_INTERVAL_S = 2.0


def _nanos(value: object) -> str:
    """An OTLP nanosecond timestamp from the SDK's ``[seconds, nanoseconds]``."""
    if isinstance(value, list) and len(value) == 2:
        seconds, nanos = value
        if isinstance(seconds, int | float) and isinstance(nanos, int | float):
            return str(int(seconds) * 1_000_000_000 + int(nanos))
    return "0"


def _any_value(value: object) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        values = [converted for item in value if (converted := _any_value(item)) is not None]
        return {"arrayValue": {"values": values}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": _key_values(value)}}
    return None


def _key_values(attributes: object) -> list[dict[str, Any]]:
    if not isinstance(attributes, dict):
        return []
    return [
        {"key": str(key), "value": converted}
        for key, value in attributes.items()
        if (converted := _any_value(value)) is not None
    ]


def _span(record: dict[str, Any]) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": record.get("traceId", ""),
        "spanId": record.get("spanId", ""),
        "name": record.get("name", ""),
        # The JS SDK's SpanKind starts at INTERNAL = 0; OTLP's starts at UNSPECIFIED = 0.
        "kind": int(record.get("kind") or 0) + 1,
        "startTimeUnixNano": _nanos(record.get("startTime")),
        "endTimeUnixNano": _nanos(record.get("endTime")),
        "attributes": _key_values(record.get("attributes")),
    }
    if parent := record.get("parentSpanId"):
        span["parentSpanId"] = parent
    status = record.get("status")
    if isinstance(status, dict):
        span["status"] = {
            key: status[key] for key in ("code", "message") if status.get(key) is not None
        }
    events = record.get("events")
    if isinstance(events, list):
        span["events"] = [
            {
                "name": event.get("name", ""),
                "timeUnixNano": _nanos(event.get("time")),
                "attributes": _key_values(event.get("attributes")),
            }
            for event in events
            if isinstance(event, dict)
        ]
    links = record.get("links")
    if isinstance(links, list):
        span["links"] = [
            {
                "traceId": (link.get("context") or {}).get("traceId", ""),
                "spanId": (link.get("context") or {}).get("spanId", ""),
                "attributes": _key_values(link.get("attributes")),
            }
            for link in links
            if isinstance(link, dict)
        ]
    return span


def to_otlp_json(lines: Iterable[bytes]) -> dict[str, Any] | None:
    """The OTLP/JSON trace export for the span lines, grouped by resource and scope, or None when
    there are none. Metric lines and anything unparseable are skipped."""
    groups: dict[tuple[str, str], tuple[dict, dict, list[dict]]] = {}
    for line in lines:
        try:
            # A writer without O_APPEND leaves NULs where the file was emptied under it.
            record = json.loads(line.lstrip(b"\0"))
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "span":
            continue
        try:
            span = _span(record)
        except (TypeError, ValueError, AttributeError):
            continue
        resource = record.get("resource") if isinstance(record.get("resource"), dict) else {}
        scope = record.get("instrumentationScope")
        scope = scope if isinstance(scope, dict) else {}
        key = (json.dumps(resource, sort_keys=True), json.dumps(scope, sort_keys=True))
        groups.setdefault(key, (resource, scope, []))[2].append(span)
    if not groups:
        return None
    resource_spans = []
    for resource, scope, spans in groups.values():
        entry: dict[str, Any] = {
            "resource": {"attributes": _key_values(resource.get("attributes"))},
            "scopeSpans": [
                {
                    "scope": {
                        key: scope[key] for key in ("name", "version") if scope.get(key) is not None
                    },
                    "spans": spans,
                }
            ],
        }
        if schema_url := resource.get("schemaUrl"):
            entry["schemaUrl"] = schema_url
        resource_spans.append(entry)
    return {"resourceSpans": resource_spans}


class SpanFileTailer:
    """Forwards the complete lines appended to ``path``, and empties it once all are forwarded.

    ``send(body)`` posts one OTLP/JSON body and says whether the gateway took it. A batch it
    rejects ``max_attempts`` times in a row is dropped, so one bad batch can't stall the rest."""

    def __init__(
        self,
        path: Path,
        send: Callable[[bytes], bool],
        *,
        max_batch_bytes: int = MAX_BATCH_BYTES,
        max_attempts: int = MAX_SEND_ATTEMPTS,
        on_drop: Callable[[str], None] = lambda _reason: None,
    ) -> None:
        self._path = path
        self._send = send
        self._max_batch_bytes = max_batch_bytes
        self._max_attempts = max_attempts
        self._on_drop = on_drop
        self._offset = 0
        self._failures = 0

    def poll(self) -> None:
        """Forward what's complete in the file now, one bounded batch at a time."""
        while self._poll_batch():
            pass

    def _poll_batch(self) -> bool:
        """Handle one batch; True when there may be more to read right away."""
        try:
            with self._path.open("rb") as file:
                size = file.seek(0, 2)
                if size < self._offset:
                    # Emptied or replaced by something else: start over.
                    self._offset = 0
                file.seek(self._offset)
                chunk = file.read(self._max_batch_bytes)
        except FileNotFoundError:
            self._offset = 0
            return False
        end = chunk.rfind(b"\n") + 1
        if end == 0:
            if len(chunk) >= self._max_batch_bytes:
                # One line longer than a whole batch can never be sent; skip it.
                self._offset += len(chunk)
                self._on_drop("a span line too large to send")
                return True
            return False
        payload = to_otlp_json(chunk[:end].splitlines())
        if payload is not None and not self._send(json.dumps(payload).encode()):
            self._failures += 1
            if self._failures < self._max_attempts:
                return False
            self._on_drop("a batch the gateway kept refusing")
        self._failures = 0
        self._offset += end
        self._truncate_if_forwarded()
        return True

    def _truncate_if_forwarded(self) -> None:
        try:
            with self._path.open("r+b") as file:
                # Only when nothing was appended since the read.
                if file.seek(0, 2) == self._offset:
                    file.truncate(0)
                    self._offset = 0
        except OSError:
            pass

    def run(self, stop: threading.Event, interval_s: float = POLL_INTERVAL_S) -> None:
        while not stop.wait(interval_s):
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 - a stray error must not end tracing
                self._on_drop(f"an error reading spans: {type(exc).__name__}")


def gateway_sender(
    client: httpx.Client,
    token: Callable[[], str],
    refresh: Callable[[], None],
    path: str,
    headers: Mapping[str, str],
) -> Callable[[bytes], bool]:
    """Posts an OTLP/JSON body to the gateway's ``path`` with a fresh Databricks token, retrying
    once with a refreshed token after a 401 or 403."""

    def send(body: bytes) -> bool:
        for attempt in range(2):
            request_headers = {
                **headers,
                "Authorization": f"Bearer {token()}",
                "Content-Type": "application/json",
            }
            try:
                response = client.post(path.lstrip("/"), content=body, headers=request_headers)
            except httpx.HTTPError:
                return False
            if response.status_code not in (401, 403) or attempt:
                return response.is_success
            try:
                refresh()
            except RuntimeError:
                return False
        return False

    return send
