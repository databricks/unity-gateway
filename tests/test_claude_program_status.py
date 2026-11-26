"""OSC output from individual agent snapshots, without a live model."""

import base64
import json
import os
import re
import threading
from dataclasses import FrozenInstanceError

import pytest

from ucode.agents import claude_program_status as status

NAMESPACE = "unity-gateway/test-launch"


def states(data):
    return re.findall(rb"\x1b\]7501;state=([a-z]+):id=([^:\x07]+)[^\x07]*\x07", data)


def reports(data):
    result = []
    for frame in re.findall(rb"\x1b\]7501;([^\x07]+)\x07", data):
        fields = dict(field.split("=", 1) for field in frame.decode("ascii").split(":"))
        if "title" in fields:
            fields["title"] = base64.b64decode(fields["title"], validate=True).decode("utf-8")
        result.append(fields)
    return result


def snapshot(*agents, session="session-1"):
    return status.SubagentSnapshot(session, tuple(status.SubagentTask(*agent) for agent in agents))


def updates(before, after):
    return reports(b"".join(after.reports_after(before, NAMESPACE)))


@pytest.mark.parametrize(
    "native,expected",
    [
        ("pending", "working"),
        ("running", "working"),
        ("waiting", "blocked"),
        ("idle", "idle"),
        ("completed", "done"),
        ("failed", "error"),
        ("killed", "clear"),
    ],
)
def test_each_agent_state_is_reported_without_aggregating_it(native, expected):
    emitted = updates(None, snapshot(("a", native, "Research")))
    assert len(emitted) == 1
    assert emitted[0]["state"] == expected
    assert emitted[0]["id"].startswith(NAMESPACE + "/")
    assert "kind" not in emitted[0]  # The registry supplies no specific input reason.


@pytest.mark.parametrize(
    "finished,outcome",
    [
        ("completed", "done"),
        ("failed", "error"),
        ("killed", "clear"),
        ("idle", "idle"),
        ("waiting", "blocked"),
    ],
)
def test_one_agent_finishing_failing_or_cancelling_does_not_replace_its_working_sibling(
    finished, outcome
):
    before = snapshot(("a", "running", "Research"), ("b", "running", "Explore"))
    initial = updates(None, before)
    after = snapshot(("a", finished, "Research"), ("b", "running", "Explore"))
    emitted = updates(before, after)
    assert len(emitted) == 1
    assert emitted[0]["state"] == outcome
    assert emitted[0]["id"] == initial[0]["id"]
    assert emitted[0]["id"] != initial[1]["id"]
    # Transitions create new snapshots, including the child rows.
    assert before.agents[0].status == "running"
    with pytest.raises(FrozenInstanceError):
        before.agents[0].status = finished


def test_failed_and_working_agents_coexist_on_the_wire():
    emitted = updates(None, snapshot(("a", "failed", "Research"), ("b", "running", "Explore")))
    assert [(r["title"], r["state"]) for r in emitted] == [
        ("Research", "error"),
        ("Explore", "working"),
    ]
    assert emitted[0]["id"] != emitted[1]["id"]


def test_removing_one_agent_clears_only_that_agent_without_claiming_completion():
    before = snapshot(("a", "running", "Research"), ("b", "running", "Explore"))
    initial = updates(None, before)
    emitted = updates(before, snapshot(("b", "running", "Explore")))
    assert len(emitted) == 1
    assert emitted[0]["state"] == "clear"
    assert emitted[0]["id"] == initial[0]["id"]
    assert emitted[0]["id"] != NAMESPACE


@pytest.mark.parametrize("next_session", ["session-1", "replacement"])
def test_ending_or_replacing_a_conversation_clears_only_our_namespace(next_session):
    before = snapshot(("a", "running", "Research"))
    ended = status.SubagentSnapshot(next_session, None)
    assert updates(before, ended) == [{"state": "clear", "id": NAMESPACE, "app": "unity-gateway"}]
    assert updates(None, ended) == []


def test_replacement_clears_old_children_before_reporting_the_new_conversation():
    before = snapshot(("a", "running", "Old work"))
    after = snapshot(("b", "running", "New work"), session="replacement")
    emitted = updates(before, after)
    assert [(r["state"], r.get("title")) for r in emitted] == [
        ("clear", None),
        ("working", "New work"),
    ]
    assert emitted[0]["id"] == NAMESPACE
    assert emitted[1]["id"].startswith(NAMESPACE + "/")


def test_reorder_and_equivalent_working_states_emit_no_extra_updates():
    before = snapshot(("a", "pending", "Research"), ("b", "running", "Explore"))
    after = snapshot(("b", "running", "Explore"), ("a", "running", "Research"))
    assert updates(before, after) == []


def test_names_and_resumed_work_keep_the_same_task_identity():
    before = snapshot(("a", "completed", "Original title"))
    initial = updates(None, before)
    emitted = updates(before, snapshot(("a", "running", "New title")))
    assert len(emitted) == 1
    assert emitted[0]["id"] == initial[0]["id"]
    assert (emitted[0]["state"], emitted[0]["title"]) == ("working", "New title")


def test_a_blocked_agent_resumes_working_under_the_same_identity():
    before = snapshot(("a", "waiting", "Research"), ("b", "running", "Explore"))
    initial = updates(None, before)
    assert [row["state"] for row in initial] == ["blocked", "working"]
    emitted = updates(before, snapshot(("a", "running", "Research"), ("b", "running", "Explore")))
    assert len(emitted) == 1
    assert emitted[0]["state"] == "working"
    assert emitted[0]["id"] == initial[0]["id"]
    assert "kind" not in initial[0]


def test_provider_ids_cannot_alias_another_agent_or_inject_terminal_controls():
    ids = ["a/b", "a_b", "a", "Research 💠" * 30, "bad\x1b]7501;state=error\x07"]
    emitted = updates(None, snapshot(*((agent_id, "running", "Task") for agent_id in ids)))
    assert len(emitted) == len(ids)
    assert len({r["id"] for r in emitted}) == len(ids)
    for row in emitted:
        assert re.fullmatch(NAMESPACE + r"/[a-f0-9]{32}", row["id"])
        assert row["state"] == "working"


def test_unicode_titles_stay_within_the_protocol_limit_and_cannot_add_fields():
    name = "Research:state=blocked\x07\x1b[31m " + "💠" * 100
    emitted = updates(None, snapshot(("a", "running", name)))
    assert len(emitted) == 1
    row = emitted[0]
    assert set(row) == {"state", "id", "app", "title"}
    assert row["state"] == "working"
    assert all(char.isprintable() for char in row["title"])
    assert len(row["title"].encode()) <= 192
    assert "💠" in row["title"]


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "null",
        "[]",
        "{}",
        '{"sessionId":"a"}',
        '{"sessionId":"","agents":[]}',
        '{"sessionId":12,"agents":[]}',
    ],
)
def test_malformed_snapshots_supply_no_completion(tmp_path, text):
    path = tmp_path / "snapshot.json"
    path.write_text(text)
    assert status.SubagentSnapshot.read(path) is None


@pytest.mark.parametrize(
    "agents",
    [
        True,
        2,
        {},
        "running",
        [None],
        [{}],
        [{"id": "a", "status": "running"}],
        [{"id": "a", "status": "unknown", "title": "Task"}],
        [{"id": "a", "status": "running", "title": 12}],
        [{"id": "", "status": "running", "title": "Task"}],
        [{"id": 12, "status": "running", "title": "Task"}],
        [{"id": "\ud800", "status": "running", "title": "Task"}],
        [
            {"id": "a", "status": "running", "title": "First"},
            {"id": "a", "status": "failed", "title": "Second"},
        ],
        [
            {"id": "valid", "status": "running", "title": "Task"},
            {"id": "bad", "status": "unknown", "title": "Task"},
        ],
    ],
)
def test_an_invalid_or_duplicate_row_cannot_silently_remove_a_known_agent(tmp_path, agents):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"sessionId": "a", "agents": agents}))
    assert status.SubagentSnapshot.read(path) is None


@pytest.mark.parametrize("agents", [None, [], [{"id": "a", "status": "failed", "title": "Task"}]])
def test_snapshot_decodes_explicit_end_empty_inventory_and_individual_tasks(tmp_path, agents):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"sessionId": "a", "agents": agents}))
    parsed = status.SubagentSnapshot.read(path)
    assert parsed.session_id == "a"
    assert (parsed.agents is None) == (agents is None)
    if agents:
        assert parsed.agents == (status.SubagentTask("a", "failed", "Task"),)
    path.unlink()
    assert status.SubagentSnapshot.read(path) is None


def test_large_agent_inventories_are_not_truncated_to_the_old_counter_payload_limit(tmp_path):
    path = tmp_path / "snapshot.json"
    agents = [
        {"id": str(i), "status": "running", "title": "Research " + "x" * 192} for i in range(200)
    ]
    path.write_text(json.dumps({"sessionId": "a", "agents": agents}))
    assert path.stat().st_size > 4096
    parsed = status.SubagentSnapshot.read(path)
    assert len(parsed.agents) == len(agents)
    assert len(parsed.reports_after(None, NAMESPACE)) == len(agents)


def test_partial_terminal_writes_do_not_drop_the_rest_of_a_record(monkeypatch):
    output = bytearray()

    def write(fd, data):
        accepted = bytes(data[:3])
        output.extend(accepted)
        return len(accepted)

    monkeypatch.setattr(status.os, "write", write)
    frame = b"\x1b]7501;state=working:id=ug/agent\x07"
    status._write_report(123, frame)
    assert output == frame


def test_a_terminal_that_accepts_no_bytes_stops_the_writer_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(status.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        status._write_report(123, b"report")


def test_temporary_terminal_backpressure_does_not_disable_reporting(monkeypatch):
    calls = []

    def write(fd, data):
        calls.append(bytes(data))
        if len(calls) == 1:
            raise BlockingIOError("terminal buffer is full")
        return len(data)

    monkeypatch.setattr(status.os, "write", write)
    status._write_report(123, b"whole report")
    assert calls == [b"whole report", b"whole report"]


def test_shutdown_cancels_a_report_waiting_for_terminal_capacity(monkeypatch):
    stopped = threading.Event()
    calls = []

    def write(fd, data):
        calls.append(bytes(data))
        stopped.set()
        raise BlockingIOError("terminal buffer is full")

    monkeypatch.setattr(status.os, "write", write)
    status._write_report(123, b"old working report", cancelled=stopped)
    assert calls == [b"old working report"]


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
            running = [
                {"id": "a", "status": "running", "title": "Research"},
                {"id": "b", "status": "running", "title": "Explore"},
            ]
            path.write_text(json.dumps({"sessionId": "a", "agents": running}))
            wait_for([b"working", b"working"])
            path.write_text("{")
            assert invalid_seen.wait(10)
            assert [state for state, _ in states(output)] == [b"working", b"working"]
            running[0] = {**running[0], "status": "completed"}
            path.write_text(json.dumps({"sessionId": "a", "agents": running}))
            wait_for([b"working", b"working", b"done"])
            running[1] = {**running[1], "status": "failed"}
            path.write_text(json.dumps({"sessionId": "a", "agents": running}))
            wait_for([b"working", b"working", b"done", b"error"])
        wait_for([b"working", b"working", b"done", b"error", b"clear"])
        emitted = reports(output)
        assert emitted[0]["id"] == emitted[2]["id"]
        assert emitted[1]["id"] == emitted[3]["id"]
        namespace = emitted[-1]["id"]
        assert re.fullmatch(r"unity-gateway/[a-f0-9]{32}", namespace)
        assert all(r["id"].startswith(namespace + "/") for r in emitted[:-1])
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
        return snapshot(("a", "running", "Late work"), session="late-session")

    monkeypatch.setattr(status.SubagentSnapshot, "read", slow_read)
    output = bytearray()

    def collect():
        while data := os.read(read_fd, 4096):
            output.extend(data)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("{}")
    try:
        with status.publish(snapshot_path, enabled=True, output_fd=write_fd) as supplied:
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
    assert [state for state, _ in states(output)] == [b"clear"]


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


def test_a_failed_second_agent_report_clears_the_already_published_first_agent(
    tmp_path, monkeypatch
):
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(status.os, "isatty", lambda fd: fd == write_fd)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", raising=False)
    original_write = os.write
    working_writes = 0

    def fail_second(fd, data):
        nonlocal working_writes
        if b"state=working" in bytes(data):
            working_writes += 1
            if working_writes == 2:
                raise OSError("terminal stopped accepting the batch")
        return original_write(fd, data)

    monkeypatch.setattr(status.os, "write", fail_second)
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
    path.write_text(
        json.dumps(
            {
                "sessionId": "a",
                "agents": [
                    {"id": "a", "status": "running", "title": "Research"},
                    {"id": "b", "status": "running", "title": "Explore"},
                ],
            }
        )
    )
    try:
        with status.publish(path, enabled=True, output_fd=write_fd) as supplied:
            supplied.start()
            with changed:
                assert changed.wait_for(
                    lambda: [s for s, _ in states(output)] == [b"working", b"clear"], timeout=10
                ), bytes(output)
        emitted = reports(output)
        assert emitted[0]["id"].startswith(emitted[1]["id"] + "/")
        assert working_writes == 2
    finally:
        os.close(write_fd)
        reader.join(timeout=10)
        os.close(read_fd)
    assert not reader.is_alive()
