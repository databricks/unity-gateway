"""Local handoff measurements: timing boundaries, failures, privacy, and opt-in behavior."""

from __future__ import annotations

import builtins
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ucode import cli, entrypoint, launch_latency, launcher, telemetry


@pytest.fixture
def capture(tmp_path, monkeypatch):
    path = tmp_path / "measurements" / "launches.jsonl"
    monkeypatch.setenv("UG_LAUNCH_LATENCY_PATH", str(path))
    monkeypatch.setattr(launch_latency, "_current", None)
    monkeypatch.setattr(telemetry, "ug_version", lambda: "1.2.3")
    monkeypatch.setattr(launch_latency.time, "perf_counter_ns", lambda: 1_250_000_000)
    yield path
    launch_latency.finish()


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("setting", [None, "", "   "])
def test_capture_requires_an_explicit_path(capture, monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("UG_LAUNCH_LATENCY_PATH")
    else:
        monkeypatch.setenv("UG_LAUNCH_LATENCY_PATH", setting)
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)
    with launch_latency.handoff():
        pass
    launch_latency.finish()
    assert not capture.exists()


def test_helpers_and_help_do_not_record_without_a_selected_launch(capture):
    launch_latency.begin(1_000_000_000)
    with launch_latency.handoff():
        pass
    launch_latency.finish()
    assert not capture.exists()


def test_handoff_is_recorded_before_execution_and_excludes_session_runtime(capture, monkeypatch):
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=True, refresh=True)
    launch_latency.update(managed=True, auto_configure=False, smart_routing=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-not-to-record")
    with launch_latency.handoff():
        (event,) = records(capture)
        assert event["duration_ms"] == 250
        assert event["outcome"] == "handoff"
        monkeypatch.setattr(launch_latency.time, "perf_counter_ns", lambda: 900_000_000_000)
    launch_latency.finish()
    assert records(capture) == [event]
    assert event["agent"] == "claude"
    assert event["managed"] is True
    assert event["skip_preflight"] is True
    assert event["auto_configure"] is False
    assert event["smart_routing"] is True
    assert event["refresh"] is True
    assert event["ug_version"] == "1.2.3"
    assert event["schema_version"] == 1
    assert "secret-not-to-record" not in capture.read_text()
    assert set(event) == {
        "schema_version",
        "event_id",
        "started_at",
        "duration_ms",
        "outcome",
        "ug_version",
        "os",
        "arch",
        "agent",
        "managed",
        "auto_configure",
        "skip_preflight",
        "refresh",
        "smart_routing",
    }


def test_pre_handoff_failure_records_once_and_preserves_early_setup_cohort(capture):
    launch_latency.begin(1_000_000_000)
    launch_latency.select("codex", skip_preflight=False)
    launch_latency.update(auto_configure=True)
    launch_latency.finish()
    launch_latency.finish()
    (event,) = records(capture)
    assert event["outcome"] == "failed"
    assert event["auto_configure"] is True
    assert "managed" not in event


def test_failed_exec_corrects_the_same_observation(capture, monkeypatch):
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)

    def fail_exec(*_args):
        monkeypatch.setattr(launch_latency.time, "perf_counter_ns", lambda: 1_300_000_000)
        raise FileNotFoundError("private native argv must not be persisted")

    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="posix", execvp=fail_exec))
    with pytest.raises(FileNotFoundError):
        launcher.exec_or_spawn(["missing-agent", "private prompt"])
    launch_latency.finish()
    original, correction = records(capture)
    assert original["event_id"] == correction["event_id"]
    assert original["outcome"] == "handoff"
    assert correction["outcome"] == "failed"
    assert correction["duration_ms"] == 300
    assert "private" not in capture.read_text()


def test_windows_wait_and_nonzero_session_exit_do_not_change_handoff(capture, monkeypatch):
    launch_latency.begin(1_000_000_000)
    launch_latency.select("codex", skip_preflight=False)
    proc = MagicMock()

    def wait():
        (event,) = records(capture)
        assert event["duration_ms"] == 250
        monkeypatch.setattr(launch_latency.time, "perf_counter_ns", lambda: 900_000_000_000)
        return 42

    proc.wait.side_effect = wait
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(launcher.subprocess_cross_os, "popen", lambda _argv: proc)
    with pytest.raises(SystemExit) as exc:
        launcher.exec_or_spawn(["codex"])
    assert exc.value.code == 42
    launch_latency.finish()
    (event,) = records(capture)
    assert event["outcome"] == "handoff"
    assert event["duration_ms"] == 250


@pytest.mark.skipif(sys.platform == "win32", reason="PTY fork is POSIX-only")
def test_pty_child_exec_failure_performs_no_post_fork_timing_io(capture, monkeypatch):
    from ucode.smart_routing import claude_pty

    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)
    socket_path = capture.parent / "prompt.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    monkeypatch.setattr(
        claude_pty, "serve_first_prompt_socket", lambda *_args, **_kwargs: SimpleNamespace()
    )
    forked = False
    original_write = launch_latency._write

    def write(*args):
        assert not forked, "timing I/O in a forked child before exec"
        original_write(*args)

    def fork():
        nonlocal forked
        forked = True
        return 0, -1

    def fail_exec(*_args):
        raise FileNotFoundError("native executable missing")

    monkeypatch.setattr(launch_latency, "_write", write)
    monkeypatch.setattr(claude_pty.pty, "fork", fork)
    monkeypatch.setattr(claude_pty.os, "execvp", fail_exec)

    with pytest.raises(FileNotFoundError, match="native executable missing"):
        claude_pty.run_claude_pty(
            ["missing-agent"], route_prompt=lambda _prompt: None, socket_path=socket_path
        )

    launch_latency.finish()
    (event,) = records(capture)
    assert event["outcome"] == "handoff"


def test_unusable_capture_path_does_not_block_execution(capture, monkeypatch):
    monkeypatch.setenv("UG_LAUNCH_LATENCY_PATH", str(capture.parent))
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)
    executed = []
    with launch_latency.handoff():
        executed.append(True)
    launch_latency.finish()
    assert executed == [True]
    assert not capture.exists()


def test_unresolvable_home_path_disables_capture(capture, monkeypatch):
    def fail_expanduser(_path):
        raise RuntimeError("unresolvable home")

    monkeypatch.setattr(launch_latency.Path, "expanduser", fail_expanduser)
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)
    with launch_latency.handoff():
        pass
    launch_latency.finish()
    assert not capture.exists()


def test_contended_capture_lock_is_nonblocking_and_does_not_block_launch(capture, monkeypatch):
    def contended(_output, *, timeout):
        assert timeout == 0
        raise TimeoutError

    monkeypatch.setattr(launch_latency, "acquire_exclusive_file_lock", contended)
    launch_latency.begin(1_000_000_000)
    launch_latency.select("claude", skip_preflight=False)
    with launch_latency.handoff():
        pass
    launch_latency.finish()
    assert capture.read_bytes() == b""


def test_entrypoint_includes_cli_import_time_and_resets_between_invocations(capture, monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr(launch_latency.time, "perf_counter_ns", lambda: clock[0])
    original_import = builtins.__import__

    def timed_import(name, *args, **kwargs):
        if name == "ucode.cli":
            clock[0] += 200_000_000
        return original_import(name, *args, **kwargs)

    def native_handoff():
        launch_latency.select("claude", skip_preflight=False)
        clock[0] += 50_000_000
        with launch_latency.handoff():
            pass

    monkeypatch.setattr(builtins, "__import__", timed_import)
    monkeypatch.setattr(sys, "argv", ["ug", "claude"])
    monkeypatch.setattr(cli, "main", native_handoff)
    entrypoint.main()
    entrypoint.main()
    first, second = records(capture)
    assert first["duration_ms"] == second["duration_ms"] == 250
    assert first["event_id"] != second["event_id"]
    assert launch_latency._current is None


def test_entrypoint_records_selected_launch_errors_and_preserves_exception(capture, monkeypatch):
    def failed_launch():
        launch_latency.select("claude", skip_preflight=False)
        raise RuntimeError("launch failure")

    monkeypatch.setattr(sys, "argv", ["ug", "claude"])
    monkeypatch.setattr(cli, "main", failed_launch)
    with pytest.raises(RuntimeError, match="launch failure"):
        entrypoint.main()
    (event,) = records(capture)
    assert event["outcome"] == "failed"
    assert launch_latency._current is None
