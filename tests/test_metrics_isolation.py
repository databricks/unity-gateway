"""Fault the telemetry boundaries while exercising real recorder and command control flow."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest

from ucode import metrics
from ucode.metrics import exporter
from ucode.metrics.exporter import send_in_background
from ucode.os_compatibility import subprocess_cross_os

HOST = "https://workspace.example.invalid"


def test_contended_recorder_drops_observations_without_blocking_command(metrics_recorder):
    finished = threading.Event()
    results = []

    def command():
        metrics.bind(HOST, "unused-token")
        metrics.start_agent("claude")
        metrics.record("ug_usage_duration_ms", 12)
        metrics.finish()
        results.append("command completed")
        finished.set()

    metrics_recorder.lock.acquire()
    caller = threading.Thread(target=command, daemon=True)
    caller.start()
    try:
        assert finished.wait(2), "command waited for the recorder lock"
        assert results == ["command completed"]
        assert not metrics_recorder.pending
        assert not metrics_recorder.credentials
    finally:
        metrics_recorder.lock.release()
        caller.join(timeout=2)


@pytest.mark.parametrize("stalled_at", ["spawn", "pipe"])
def test_stalled_uploader_does_not_block_handoff_or_exit(metrics_recorder, monkeypatch, stalled_at):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    results = []
    export_threads = []

    def stall():
        export_threads.append(threading.current_thread())
        entered.set()
        assert release.wait(5), "test did not release the uploader"

    class Pipe(io.BytesIO):
        def write(self, payload):
            if stalled_at == "pipe":
                stall()
            return super().write(payload)

    child = Mock(stdin=Pipe())
    child.poll.return_value = None

    def spawn(*args, **kwargs):
        if stalled_at == "spawn":
            stall()
        return child

    # Only the OS process/pipe boundary is controlled; serialization and recorder run normally.
    monkeypatch.setattr(exporter, "send_in_background", send_in_background)
    monkeypatch.setattr(subprocess_cross_os, "popen", Mock(side_effect=spawn))
    metrics.bind(HOST, "unused-token")
    metrics.record("ug_usage_duration_ms", 12)
    metrics.flush()

    def command():
        metrics.start_agent("claude")
        for n in range(1500):
            metrics.record("proxy_response_bytes", n)
            metrics.flush()
        metrics.agent_handoff()
        metrics.finish()
        results.append("command completed")
        finished.set()

    caller = threading.Thread(target=command, daemon=True)
    try:
        assert entered.wait(2)
        caller.start()
        assert finished.wait(2), "command waited for the uploader"
        assert results == ["command completed"]
        assert len(metrics_recorder.pending) == 1000
        assert export_threads == [metrics_recorder.thread]
        assert metrics_recorder.thread.daemon
        assert subprocess_cross_os.popen.call_count == 1
    finally:
        release.set()
        metrics_recorder.stop.set()
        metrics_recorder.wake.set()
        metrics_recorder.thread.join(timeout=2)
        if caller.ident is not None:
            caller.join(timeout=2)
    assert not metrics_recorder.thread.is_alive()


def test_process_exits_while_export_worker_is_stalled():
    # A subprocess proves Python itself does not join the worker during shutdown.
    code = """
import os
import threading
from ucode import metrics
from ucode.metrics import exporter
entered = threading.Event()
stalled = threading.Event()
def upload(*args):
    entered.set()
    stalled.wait()
exporter.send_in_background = upload
os.environ['UG_ENABLE_METRICS_EXPORT'] = '1'
metrics.begin('usage')
metrics.record('ug_usage_duration_ms', 12)
metrics.flush()
assert entered.wait(2)
metrics.finish()
print('command completed')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "command completed\n"
    assert result.stderr == ""


@pytest.mark.parametrize("fault", ["clock", "thread"])
def test_initialization_failure_leaves_command_uninstrumented(monkeypatch, fault):
    monkeypatch.setattr(metrics, "_runtime", None)
    monkeypatch.setenv("UG_ENABLE_METRICS_EXPORT", "1")
    if fault == "clock":
        monkeypatch.setattr(metrics.time, "perf_counter", Mock(side_effect=OSError("clock failed")))
    else:
        monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("no threads")))

    metrics.begin("usage")
    calls = []

    @metrics.timed("ug_usage_duration_ms")
    def command():
        calls.append("called")
        return "result"

    assert command() == "result"
    assert calls == ["called"]
    assert metrics._runtime is None
    metrics.finish()


@pytest.mark.parametrize("fault_at", ["setup", "cleanup"])
@pytest.mark.parametrize("raises", [False, True])
def test_timing_failure_preserves_result_and_original_error(
    metrics_recorder, monkeypatch, fault_at, raises
):
    ticks = [OSError("clock failed")] if fault_at == "setup" else [1, OSError("clock failed")]
    monkeypatch.setattr(metrics.time, "perf_counter", Mock(side_effect=ticks))
    original = RuntimeError("command failed")
    result = object()
    calls = []

    @metrics.timed("ug_usage_duration_ms")
    def command():
        calls.append("called")
        if raises:
            raise original
        return result

    if raises:
        with pytest.raises(RuntimeError) as error:
            command()
        assert error.value is original
    else:
        assert command() is result
    assert calls == ["called"]
    assert metrics._scope.get() is None


def test_failed_process_creation_is_isolated_and_worker_can_export_again(
    metrics_recorder, monkeypatch
):
    attempted = threading.Event()
    delivered = threading.Event()

    class Pipe(io.BytesIO):
        def close(self):
            self.payload = self.getvalue()
            super().close()
            delivered.set()

    child = Mock(stdin=Pipe())
    child.poll.return_value = 0
    attempts = []

    def spawn(*args, **kwargs):
        attempts.append(threading.current_thread())
        if len(attempts) == 1:
            attempted.set()
            raise OSError("process creation failed")
        return child

    monkeypatch.setattr(exporter, "send_in_background", send_in_background)
    monkeypatch.setattr(subprocess_cross_os, "popen", spawn)
    metrics.bind(HOST, "unused-token")
    metrics.record("ug_usage_duration_ms", 12)
    metrics.flush()
    assert attempted.wait(2)
    metrics.record("ug_usage_duration_ms", 24)
    metrics.flush()
    assert delivered.wait(2)
    assert all(t is metrics_recorder.thread for t in attempts)
    assert json.loads(child.stdin.payload)[0]["observations"] == [
        ["ug_usage_duration_ms", 24, {"command": "claude"}]
    ]
