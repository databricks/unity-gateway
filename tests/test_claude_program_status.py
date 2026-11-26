"""OSC output from structured delegated-work snapshots, without a live agent."""

import json
import os
import re
import threading

import pytest

from ucode.agents import claude_program_status as status


def states(data):
    return re.findall(rb"\x1b\]7501;state=([a-z]+):id=([^:\x07]+)[^\x07]*\x07", data)


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (None, ("a", 0), []),
        (None, ("a", 2), [b"working"]),
        (("a", 2), ("a", 1), []),
        (("a", 1), ("a", 0), [b"done"]),
        (("a", 0), ("a", 2), [b"working"]),
        (("a", 2), ("a", None), [b"clear"]),
        (("a", 2), ("b", 0), [b"clear"]),
        (("a", 2), ("b", 1), [b"clear", b"working"]),
        (("a", None), ("a", 1), [b"working"]),
    ],
)
def test_only_finishing_observed_delegated_work_reports_done(before, after, expected):
    previous = status.SubagentSnapshot(*before) if before else None
    output = status.SubagentSnapshot(*after).reports_after(previous, "unity-gateway/test")
    assert [state for state, _ in states(output)] == expected
    assert all(task == b"unity-gateway/test" for _, task in states(output))


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "null",
        "[]",
        "{}",
        '{"sessionId":"a"}',
        '{"sessionId":"","activeTasks":1}',
        '{"sessionId":12,"activeTasks":1}',
        '{"sessionId":"a","activeTasks":true}',
        '{"sessionId":"a","activeTasks":-1}',
        '{"sessionId":"a","activeTasks":1.5}',
        '{"sessionId":"a","activeTasks":"1"}',
        '{"sessionId":"a","activeTasks":1}' + " " * 4096 + "x",
    ],
)
def test_malformed_snapshots_supply_no_completion(tmp_path, text):
    path = tmp_path / "snapshot.json"
    path.write_text(text)
    assert status.SubagentSnapshot.read(path) is None


@pytest.mark.parametrize("count", [None, 0, 1, 300])
def test_snapshot_decodes_explicit_end_and_task_counts(tmp_path, count):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"sessionId": "a", "activeTasks": count}))
    assert status.SubagentSnapshot.read(path) == status.SubagentSnapshot("a", count)
    path.unlink()
    assert status.SubagentSnapshot.read(path) is None


def test_worker_publishes_wire_reports_and_retains_work_through_bad_reads(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(status.os, "isatty", lambda fd: fd == write_fd)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", raising=False)
    output = bytearray()
    changed = threading.Condition()

    def collect():
        while data := os.read(read_fd, 4096):
            with changed:
                output.extend(data)
                changed.notify_all()

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    path = tmp_path / "snapshot.json"
    invalid_seen = threading.Event()
    read = status.SubagentSnapshot.read

    def observe_read(path):
        snapshot = read(path)
        if snapshot is None and path.exists():
            invalid_seen.set()
        return snapshot

    monkeypatch.setattr(status.SubagentSnapshot, "read", observe_read)

    def wait_for(expected):
        with changed:
            assert changed.wait_for(
                lambda: [state for state, _ in states(output)] == expected, timeout=10
            ), bytes(output)

    try:
        with status.publish(path, enabled=True, output_fd=write_fd) as supplied:
            assert supplied.path == path
            supplied.start()
            path.write_text('{"sessionId":"a","activeTasks":2}')
            wait_for([b"working"])
            path.write_text("{")
            assert invalid_seen.wait(10)
            assert [state for state, _ in states(output)] == [b"working"]
            path.write_text('{"sessionId":"a","activeTasks":0}')
            wait_for([b"working", b"done"])
            path.write_text('{"sessionId":"a","activeTasks":1}')
            wait_for([b"working", b"done", b"working"])
        wait_for([b"working", b"done", b"working", b"clear"])
        ids = {task for _, task in states(output)}
        assert len(ids) == 1
        assert re.fullmatch(rb"unity-gateway/[a-f0-9]{32}", ids.pop())
    finally:
        os.close(write_fd)
        reader.join(timeout=10)
        os.close(read_fd)
    assert not reader.is_alive()


def test_a_read_finishing_after_shutdown_cannot_publish_work(tmp_path, monkeypatch):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(status.os, "isatty", lambda fd: fd == write_fd)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", raising=False)
    entered = threading.Event()
    release = threading.Event()

    def slow_read(path):
        entered.set()
        assert release.wait(10)
        return status.SubagentSnapshot("late-session", 2)

    monkeypatch.setattr(status.SubagentSnapshot, "read", slow_read)
    output = bytearray()

    def collect():
        while data := os.read(read_fd, 4096):
            output.extend(data)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        with status.publish(
            tmp_path / "snapshot.json", enabled=True, output_fd=write_fd
        ) as supplied:
            supplied.start()
            assert entered.wait(10)
        # Shutdown returned while the filesystem read was held. Releasing that
        # old read must close the worker's descriptor without reporting work.
        release.set()
    finally:
        release.set()
        os.close(write_fd)
        reader.join(timeout=10)
        os.close(read_fd)
    assert not reader.is_alive()
    assert output == b""


def test_preparing_a_publication_does_not_start_a_worker_before_the_child_spawn(
    tmp_path, monkeypatch
):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(status.os, "isatty", lambda fd: fd == write_fd)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", raising=False)
    reads = []
    monkeypatch.setattr(status.SubagentSnapshot, "read", lambda path: reads.append(path))
    try:
        with status.publish(
            tmp_path / "snapshot.json", enabled=True, output_fd=write_fd
        ) as supplied:
            assert supplied.path is not None
        # No child was spawned, so activation was never requested. The duplicate
        # descriptor still closes on exit, without ever creating a reader.
        os.close(write_fd)
        write_fd = None
        output = []
        reader = threading.Thread(target=lambda: output.append(os.read(read_fd, 4096)), daemon=True)
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive(), "the unused publisher leaked its output descriptor"
        assert output == [b""]
        assert reads == []
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize(
    "enabled,tty,opt_out", [(False, True, ""), (True, False, ""), (True, True, "1")]
)
def test_headless_disabled_and_opted_out_launches_do_not_publish(
    tmp_path, monkeypatch, enabled, tty, opt_out
):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", opt_out)
    monkeypatch.setattr(status.os, "isatty", lambda fd: tty)
    path = tmp_path / "snapshot.json"
    with status.publish(path, enabled=enabled, output_fd=1) as supplied:
        assert supplied.path is None
        supplied.start()
    assert not path.exists()
