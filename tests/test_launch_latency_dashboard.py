"""Offline component checks for launch-record ingestion and dashboard generation."""

from __future__ import annotations

import importlib.util
import json
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from ucode.os_compatibility import subprocess_cross_os

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "launch_latency_dashboard.py"
SPEC = importlib.util.spec_from_file_location("launch_latency_dashboard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def record(**overrides):
    return {
        "schema_version": 1,
        "event_id": "launch-a",
        "started_at": "2026-11-05T12:00:00.000Z",
        "duration_ms": 1250.5,
        "outcome": "handoff",
        "agent": "claude",
        "ug_version": "0.1.0",
        "os": "linux",
        "arch": "x86_64",
        "managed": True,
        "auto_configure": False,
        "skip_preflight": False,
        "refresh": False,
        "smart_routing": True,
        **overrides,
    }


def write_records(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class DashboardDocument(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.scripts = []
        self._current_script = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._current_script = {"attrs": dict(attrs), "body": ""}
            self.scripts.append(self._current_script)

    def handle_data(self, data):
        if self._current_script is not None:
            self._current_script["body"] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self._current_script = None

    @property
    def payload(self):
        payloads = [script for script in self.scripts if script["attrs"].get("id") == "launch-data"]
        assert len(payloads) == 1
        assert payloads[0]["attrs"]["type"] == "application/json"
        return json.loads(payloads[0]["body"])


@pytest.mark.parametrize(
    "duration",
    [-1, float("nan"), float("inf"), float("-inf"), True, False, None, "1200", 10**400],
    ids=[
        "negative",
        "nan",
        "infinity",
        "negative-infinity",
        "true",
        "false",
        "null",
        "text",
        "overflow",
    ],
)
def test_invalid_durations_do_not_pollute_launch_samples(tmp_path, duration):
    path = tmp_path / "launches.jsonl"
    write_records(path, [record(duration_ms=duration), record(event_id="zero", duration_ms=0)])

    records, ignored, duplicates = dashboard.load_records(path)

    assert [(row["event_id"], row["duration_ms"]) for row in records] == [("zero", 0)]
    assert (ignored, duplicates) == (1, 0)


def test_malformed_and_unsupported_rows_leave_valid_observations_available(tmp_path):
    path = tmp_path / "launches.jsonl"
    unsupported = [
        None,
        [],
        record(schema_version=2),
        record(outcome="ready"),
        record(started_at="yesterday"),
        record(started_at="2026-11-05T12:00:00"),
        record(started_at=None),
    ]
    path.write_bytes(
        b"\n  \n\xff\n"
        + "".join(json.dumps(row) + "\n" for row in unsupported).encode()
        + json.dumps(record()).encode()
        + b'\n{"duration_ms":'
    )

    records, ignored, duplicates = dashboard.load_records(path)

    assert len(records) == 1
    assert records[0]["event_id"] == "launch-a"
    assert (ignored, duplicates) == (len(unsupported) + 2, 0)


def test_timestamps_are_normalized_before_sorting_and_dimensions_keep_unknowns(tmp_path):
    path = tmp_path / "launches.jsonl"
    earlier = record(
        event_id="earlier",
        started_at="2026-11-05T13:30:00+02:00",
        agent=None,
        ug_version="",
        os=123,
        arch=False,
        managed="true",
        auto_configure=True,
        skip_preflight=0,
        refresh=None,
        smart_routing="false",
    )
    write_records(path, [record(), earlier])

    records, ignored, duplicates = dashboard.load_records(path)

    assert [row["event_id"] for row in records] == ["earlier", "launch-a"]
    normalized = records[0]
    assert normalized["started_at"] == "2026-11-05T11:30:00.000+00:00"
    assert all(normalized[key] == "unknown" for key in ("agent", "ug_version", "os", "arch"))
    assert normalized["auto_configure"] is True
    assert all(
        normalized[key] is None for key in ("managed", "skip_preflight", "refresh", "smart_routing")
    )
    assert records[1]["managed"] is True
    assert records[1]["skip_preflight"] is False
    assert (ignored, duplicates) == (0, 0)


def test_latest_valid_correction_replaces_handoff_without_double_counting(tmp_path):
    path = tmp_path / "launches.jsonl"
    anonymous = record(duration_ms=2000)
    del anonymous["event_id"]
    write_records(
        path,
        [
            record(),
            record(event_id="launch-b", started_at="2026-11-05T11:00:00Z"),
            anonymous,
            anonymous,
            record(outcome="failed", duration_ms=1300),
            record(outcome="handoff", duration_ms=float("nan")),
        ],
    )

    records, ignored, duplicates = dashboard.load_records(path)

    assert len(records) == 4
    corrected = [row for row in records if row.get("event_id") == "launch-a"]
    assert len(corrected) == 1
    assert (corrected[0]["outcome"], corrected[0]["duration_ms"]) == ("failed", 1300)
    assert sum(row["outcome"] == "handoff" for row in records) == 3
    assert sum("event_id" not in row for row in records) == 2
    assert records[0]["event_id"] == "launch-b"
    assert (ignored, duplicates) == (1, 1)


def test_script_delimiters_stay_data_and_unknown_fields_are_not_exported(tmp_path):
    path = tmp_path / "launches.jsonl"
    label = '</script><script>alert("launch")</script><img src=x onerror=alert(1)> & café'
    write_records(
        path,
        [
            record(
                agent=label,
                token="private-test-token",
                prompt="private-test-prompt",
                workspace="https://private-test-workspace.example",
                environment={"PASSWORD": "private-test-password"},
            )
        ],
    )
    records, ignored, duplicates = dashboard.load_records(path)

    html = dashboard.render_dashboard(
        records, ignored=ignored, duplicates=duplicates, generated_at="2026-11-05T14:00:00Z"
    )
    document = DashboardDocument(html)

    assert len(document.scripts) == 2
    assert all("src" not in script["attrs"] for script in document.scripts)
    assert document.payload["records"][0]["agent"] == label
    assert document.payload["generated_at"] == "2026-11-05T14:00:00Z"
    assert label not in html
    assert "private-test-" not in html
    assert all(
        key not in document.payload["records"][0]
        for key in ("token", "prompt", "workspace", "environment")
    )


def test_missing_input_can_generate_an_empty_offline_dashboard(tmp_path):
    source = tmp_path / "not-recorded-yet.jsonl"
    destination = tmp_path / "reports" / "launches.html"

    result = subprocess_cross_os.run(
        [sys.executable, str(SCRIPT), "--input", str(source), "--output", str(destination)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not source.exists()
    document = DashboardDocument(destination.read_text(encoding="utf-8"))
    assert document.payload["records"] == []
    assert document.payload["ignored"] == document.payload["duplicates"] == 0
    assert all("src" not in script["attrs"] for script in document.scripts)
    assert "0 launch observations" in result.stdout


def test_cli_preserves_records_when_output_is_the_input_file(tmp_path):
    source = tmp_path / "launches.jsonl"
    write_records(source, [record()])
    original = source.read_bytes()

    result = subprocess_cross_os.run(
        [sys.executable, str(SCRIPT), "--input", str(source), "--output", str(source)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "--output must differ from --input" in result.stderr
    assert source.read_bytes() == original
