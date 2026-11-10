import hashlib
import json
import os
import stat
import time
import uuid

import pytest

from ucode.smart_routing import claude_routing, routing, session_log, v2


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setattr(session_log, "LOG_DIR", directory)
    return directory


@pytest.fixture
def decision():
    return routing.RoutingDecision(
        "selected", "reason", (("selected", "claude"), ("other", None)), "stable"
    )


def write(payload, decision, task="inspect parser", **kwargs):
    session_log.append_decision(
        "claude", payload, task, decision, "requested", routing_ms=840, **kwargs
    )


def test_record_shape(log_dir, decision):
    payload = {
        "session_id": "s1",
        "tool_use_id": "tool1",
        "tool_input": {"subagent_type": "Explore"},
    }
    before = time.time()
    write(payload, decision)
    record = json.loads((log_dir / "claude-s1.jsonl").read_text())
    assert uuid.UUID(hex=record.pop("decision_id")).version == 4
    assert before <= record.pop("at") <= time.time()
    assert record == {
        "v": 1,
        "type": "decision",
        "harness": "claude",
        "kind": "subagent",
        "session_id": "s1",
        "tool_use_id": "tool1",
        "prompt": {
            "preview": "inspect parser",
            "sha256": hashlib.sha256(b"inspect parser").hexdigest(),
            "length": 14,
        },
        "original": {"model": None, "subagent_type": "Explore"},
        "selected_model": "selected",
        "requested_model": "requested",
        "candidates": [
            {"model": "selected", "harness": "claude"},
            {"model": "other", "harness": None},
        ],
        "router_name": "stable",
        "routing_ms": 840,
        "rationale": "reason",
        "raw_rationale": None,
    }


def test_explanation_key_present_when_set_and_absent_when_none(log_dir, decision):
    payload = {"session_id": "s1"}
    write(payload, decision)
    explained = routing.RoutingDecision(
        "selected",
        "reason",
        (("selected", "claude"),),
        "stable",
        explanation={"models": [{"id": "selected"}]},
    )
    write(payload, explained)
    first, second = [
        json.loads(line) for line in (log_dir / "claude-s1.jsonl").read_text().splitlines()
    ]
    assert "explanation" not in first
    assert second["explanation"] == {"models": [{"id": "selected"}]}


def test_preview_first_nonblank_line_truncated(log_dir, decision):
    task = "\n  \n  " + "x" * 161 + "  \nsecond line"
    write({"session_id": "s1"}, decision, task)
    record = json.loads((log_dir / "claude-s1.jsonl").read_text())
    assert record["prompt"]["preview"] == "x" * 160 + "…"
    assert record["prompt"]["length"] == len(task)
    assert record["tool_use_id"] is None


@pytest.mark.parametrize("session_id", ["../escape", "a" * 129, "a/b", "é"])
def test_unsafe_ids_are_hashed(log_dir, decision, session_id):
    write({"session_id": session_id}, decision)
    filename = "claude-" + hashlib.sha256(session_id.encode()).hexdigest() + ".jsonl"
    assert [path.name for path in log_dir.iterdir()] == [filename]
    assert json.loads((log_dir / filename).read_text())["session_id"] == session_id


@pytest.mark.parametrize("session_id", [None, "", 123, []])
def test_missing_ids_do_not_write(log_dir, decision, session_id):
    write({"session_id": session_id}, decision)
    assert not log_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_private_permissions(log_dir, decision):
    write({"session_id": "s1"}, decision)
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((log_dir / "claude-s1.jsonl").stat().st_mode) == 0o600


def test_retention_only_on_new_file(log_dir, decision):
    write({"session_id": "s1"}, decision)
    old = log_dir / "old.jsonl"
    recent = log_dir / "recent.jsonl"
    old.write_text("old")
    recent.write_text("recent")
    expired = time.time() - session_log.RETENTION_SECONDS - 10
    os.utime(old, (expired, expired))
    write({"session_id": "s1"}, decision)
    assert old.exists()
    write({"session_id": "s2"}, decision)
    assert not old.exists()
    assert recent.exists()
    assert len((log_dir / "claude-s1.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("failure", ["mkdir", "open", "lock"])
def test_filesystem_errors_are_nonfatal(log_dir, decision, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError("unavailable")

    if failure == "mkdir":
        monkeypatch.setattr(type(log_dir), "mkdir", fail)
    elif failure == "open":
        monkeypatch.setattr(session_log.os, "open", fail)
    else:
        monkeypatch.setattr(session_log, "acquire_exclusive_file_lock", fail)
    write({"session_id": "s1"}, decision)
    path = log_dir / "claude-s1.jsonl"
    if failure == "lock":
        # The file was created before the lock failed, but no record reached it.
        assert path.exists() and path.read_text() == ""
    else:
        assert not path.exists()


def test_claude_subagent_audit_writes_one_timed_record(log_dir, tmp_path, decision, monkeypatch):
    monkeypatch.setattr(v2.claude_routing, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(v2, "_request_claude_routing_decision", lambda *args: (decision, None))
    payload = {
        "session_id": "s1",
        "tool_name": "Agent",
        "tool_use_id": "call1",
        "tool_input": {"prompt": "inspect parser", "model": "sonnet", "subagent_type": "Explore"},
    }
    result = v2.route_claude_subagent(
        payload,
        workspace="https://example.com",
        token="token",
        available_models=["selected"],
        audit_decision=True,
    )
    assert result == {"model": "selected", "rationale": "reason"}
    records = (log_dir / "claude-s1.jsonl").read_text().splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert type(record["routing_ms"]) is int and record["routing_ms"] >= 0
    assert record["original"] == {"model": "sonnet", "subagent_type": "Explore"}
    assert record["tool_use_id"] == "call1"
    assert json.loads((tmp_path / "decisions.jsonl").read_text())["requested_model"] == "selected"


def test_no_session_record_without_audit_or_route(log_dir, decision, monkeypatch):
    monkeypatch.setattr(v2, "_request_claude_routing_decision", lambda *args: (decision, None))
    payload = {"session_id": "s1", "tool_name": "Agent", "tool_input": {"prompt": "inspect"}}
    kwargs = {
        "workspace": "https://example.com",
        "token": "token",
        "available_models": ["selected"],
    }
    v2.route_claude_subagent(payload, **kwargs)
    v2.route_claude_subagent({**payload, "tool_name": "Read"}, audit_decision=True, **kwargs)
    assert not log_dir.exists()


def test_read_decisions_distinguishes_missing_from_unreadable(log_dir, decision, monkeypatch):
    """A missing log is empty history; a permission failure must not look empty."""

    def deny_read(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    assert session_log.read_decisions("claude", "absent") == []
    write({"session_id": "s1"}, decision)
    path = log_dir / "claude-s1.jsonl"
    monkeypatch.setattr(type(path), "read_text", deny_read)
    with pytest.raises(PermissionError):
        session_log.read_decisions("claude", "s1")


def test_clear_routing_artifacts_removes_session_logs(log_dir, decision, tmp_path, monkeypatch):
    monkeypatch.setattr(claude_routing, "CANARY_PATH", tmp_path / "canary.json")
    monkeypatch.setattr(claude_routing, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(claude_routing, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    write({"session_id": "s1"}, decision)
    assert log_dir.exists()
    claude_routing.clear_routing_artifacts()
    assert not log_dir.exists()
