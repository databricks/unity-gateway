"""Fault the telemetry boundaries while exercising real recorder and command control flow."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from ucode import metrics
from ucode.metrics import exporter
from ucode.metrics.exporter import start_exporter
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
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    child = Mock(stdin=os.fdopen(write_fd, "wb", buffering=0))
    child.poll.return_value = None
    spawn_threads = []

    def spawn(*args, **kwargs):
        spawn_threads.append(threading.current_thread())
        if stalled_at == "pipe":
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
        entered.set()
        if stalled_at == "spawn":
            assert release.wait(5), "test did not release the uploader"
        return child

    monkeypatch.setattr(exporter, "start_exporter", start_exporter)
    monkeypatch.setattr(subprocess_cross_os, "popen", Mock(side_effect=spawn))
    metrics_recorder.start_periodic()

    def command():
        metrics.bind(HOST, "unused-token")
        metrics.start_agent("claude")
        for n in range(1500):
            metrics.record("proxy_response_bytes", n)
            metrics.flush()
        metrics.agent_handoff()
        metrics.finish()
        finished.set()

    caller = threading.Thread(target=command, daemon=True)
    try:
        assert entered.wait(2)
        caller.start()
        assert finished.wait(2), "command waited for the uploader"
        assert len(metrics_recorder.pending) <= 1000
        assert spawn_threads == [metrics_recorder.thread]
        assert metrics_recorder.thread.daemon
        assert subprocess_cross_os.popen.call_count == 1
    finally:
        release.set()
        metrics_recorder.close()
        metrics_recorder.thread.join(timeout=2)
        if caller.ident is not None:
            caller.join(timeout=2)
        child.stdin.close()
        os.close(read_fd)
    assert not metrics_recorder.thread.is_alive()


def test_process_exits_while_export_worker_is_stalled():
    # A subprocess proves Python itself does not join the worker during shutdown.
    code = """
import os
import threading
import time
from ucode import metrics
from ucode.metrics import exporter
entered = threading.Event()
stalled = threading.Event()
def upload(*args):
    entered.set()
    stalled.wait()
exporter.start_exporter = upload
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
    read_fd, write_fd = os.pipe()
    child = Mock(stdin=os.fdopen(write_fd, "wb", buffering=0))
    child.poll.return_value = None
    attempts = []

    def spawn(*args, **kwargs):
        attempts.append(threading.current_thread())
        if len(attempts) == 1:
            attempted.set()
            raise OSError("process creation failed")
        return child

    monkeypatch.setattr(exporter, "start_exporter", start_exporter)
    monkeypatch.setattr(subprocess_cross_os, "popen", spawn)
    try:
        metrics.bind(HOST, "unused-token")
        metrics.record("ug_usage_duration_ms", 12)
        metrics_recorder.start_periodic()
        assert attempted.wait(2)
        metrics.record("ug_usage_duration_ms", 24)
        deadline = time.monotonic() + 2
        while metrics_recorder.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not metrics_recorder.pending
        assert all(t is metrics_recorder.thread for t in attempts)
    finally:
        metrics_recorder.close()
        metrics_recorder.thread.join(timeout=2)
        child.stdin.close()
    with os.fdopen(read_fd, "rb") as reader:
        buffer = exporter.ExportBuffer()
        buffer.read(reader)
    assert buffer.drain()[0]["observations"] == [
        ("ug_usage_duration_ms", 12, {"command": "claude"}),
        ("ug_usage_duration_ms", 24, {"command": "claude"}),
    ]
