"""Offline diagnostics checks; no workspace or agent inference is exercised."""

from __future__ import annotations

import json
import sys
from unittest.mock import Mock

import pytest

from ucode import startup_timing


def _records(stderr: str) -> list[dict]:
    return [json.loads(line) for line in stderr.splitlines()]


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_disabled_timing_does_not_read_clock_or_write(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("UG_STARTUP_TIMING", raising=False)
    else:
        monkeypatch.setenv("UG_STARTUP_TIMING", value)
    clock = Mock(side_effect=AssertionError("disabled timer read the clock"))
    monkeypatch.setattr(startup_timing.time, "perf_counter_ns", clock)
    with startup_timing.startup_phase("authentication"):
        startup_timing.startup_mark("launch_start")
    assert capsys.readouterr() == ("", "")
    clock.assert_not_called()


def test_phase_records_boundaries_and_elapsed_time(monkeypatch, capsys):
    monkeypatch.setenv("UG_STARTUP_TIMING", "1")
    monkeypatch.setattr(startup_timing, "_STARTED_NS", 1_000_000)
    monkeypatch.setattr(
        startup_timing.time, "perf_counter_ns", iter([2_000_000, 2_000_000, 7_000_000]).__next__
    )
    with startup_timing.startup_phase("authentication"):
        print("protocol response")
    captured = capsys.readouterr()
    assert captured.out == "protocol response\n"
    started, completed = _records(captured.err)
    assert started["outcome"] == "started"
    assert started["elapsed_ms"] == 1
    assert completed["duration_ms"] == 5
    assert completed["elapsed_ms"] == 6
    assert completed["outcome"] == "ok"
    for record in [started, completed]:
        assert set(record) == {"event", "stage", "outcome", "pid", "elapsed_ms", "duration_ms"}
        assert record["event"] == "ug_startup_timing"
        assert record["stage"] == "authentication"
        assert isinstance(record["pid"], int)


@pytest.mark.parametrize("failure", [RuntimeError("secret-token"), KeyboardInterrupt()])
def test_failure_is_preserved_without_its_message(monkeypatch, capsys, failure):
    monkeypatch.setenv("UG_STARTUP_TIMING", "1")
    with pytest.raises(type(failure)) as raised:
        with startup_timing.startup_phase("model_discovery"):
            raise failure
    assert raised.value is failure
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "secret-token" not in captured.err
    assert [row["outcome"] for row in _records(captured.err)] == ["started", "error"]


@pytest.mark.parametrize("failure", [None, RuntimeError("original failure")])
def test_closed_stderr_never_changes_launch_outcome(monkeypatch, failure):
    monkeypatch.setenv("UG_STARTUP_TIMING", "1")
    monkeypatch.setattr(sys, "stderr", Mock(write=Mock(side_effect=OSError("closed"))))
    if failure is None:
        with startup_timing.startup_phase("configure_tool"):
            startup_timing.startup_mark("agent_prepare")
    else:
        with pytest.raises(RuntimeError) as raised:
            with startup_timing.startup_phase("configure_tool"):
                raise failure
        assert raised.value is failure
