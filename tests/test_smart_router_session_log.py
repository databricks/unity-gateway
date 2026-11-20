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


def write(payload, decision, task="inspect parser"):
    session_log.append_decision("claude", payload, task, decision, "requested", routing_ms=840)


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
    monkeypatch.setattr(
        v2, "_request_claude_routing_decision", lambda *args, **kwargs: (decision, None)
    )
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
    monkeypatch.setattr(
        v2, "_request_claude_routing_decision", lambda *args, **kwargs: (decision, None)
    )
    payload = {"session_id": "s1", "tool_name": "Agent", "tool_input": {"prompt": "inspect"}}
    kwargs = {
        "workspace": "https://example.com",
        "token": "token",
        "available_models": ["selected"],
    }
    v2.route_claude_subagent(payload, **kwargs)
    v2.route_claude_subagent({**payload, "tool_name": "Read"}, audit_decision=True, **kwargs)
    assert not log_dir.exists()


def test_clear_routing_artifacts_removes_session_logs(log_dir, decision, tmp_path, monkeypatch):
    monkeypatch.setattr(claude_routing, "CANARY_PATH", tmp_path / "canary.json")
    monkeypatch.setattr(claude_routing, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(claude_routing, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    write({"session_id": "s1"}, decision)
    assert log_dir.exists()
    claude_routing.clear_routing_artifacts()
    assert not log_dir.exists()


def listed(**overrides):
    return {
        "v": 1,
        "type": "decision",
        "decision_id": "abcd1234",
        "at": 1000.0,
        "prompt": {"preview": "line one\n  line two"},
        "selected_model": "m",
        "routing_ms": 5,
        **overrides,
    }


def box(*lines):
    return ["┌" + "─" * 75 + "┐", *(f"│ {line:<73} │" for line in lines), "└" + "─" * 75 + "┘"]


def test_render_table_formats_age_and_collapses_whitespace():
    lines = session_log.render_table([listed()], now=1000.0 + 7200).splitlines()
    assert lines[2:-1] == box(
        "ID        WHEN    TASK               MODEL  ROUTING",
        "abcd1234  2h ago  line one line two  m      5 ms",
    )


def test_render_table_with_no_listable_records_is_boxed_empty_message():
    text = session_log.render_table([{"v": 1, "type": "decision"}])
    assert text.splitlines() == box("No routed subagents recorded this session yet.")


def test_render_table_shrinks_task_column_to_fit_the_box():
    long = listed(prompt={"preview": "t" * 200}, selected_model="system.ai.gpt-5-6-sol")
    lines = session_log.render_table([long], now=1000.0).splitlines()
    assert lines[0] == "1 routed subagents this session (showing last 1)"
    assert lines[1] == ""
    assert lines[-1] == "Show one: $smart-router logs --id <ID>"
    assert all(len(line) == 77 for line in lines[2:-1])
    # 73 columns minus ID 8, WHEN 8, MODEL 11, ROUTING 7 and four 2-column gaps leaves 31 for TASK.
    assert lines[4] == f"│ abcd1234  just now  {'t' * 30}…  gpt-5-6-sol  5 ms    │"


def test_render_table_truncates_model_only_after_task_hits_its_minimum():
    wide = listed(prompt={"preview": "t" * 200}, selected_model="m" * 80)
    lines = session_log.render_table([wide], now=1000.0).splitlines()
    assert all(len(line) == 77 for line in lines[2:-1])
    assert lines[4].count("…") == 2
    assert f"  {'t' * 11}…  " in lines[4]


def test_render_detail_exact_boxed_layout_with_hanging_indent():
    record = {
        "decision_id": "e789d49a" + "0" * 24,
        "at": 1000.0,
        "prompt": {"preview": "Investigate the service named ai-gateway. " * 3},
        "original": {"subagent_type": None},
        "selected_model": "system.ai.databricks-gpt-5-6-terra",
        "routing_ms": 4040,
        "rationale": "This task involves several related components. " * 2,
        "candidates": [
            {"model": "system.ai.gpt-6-astra"},
            {"model": "system.ai.databricks-gpt-5-6-terra"},
        ],
    }
    assert session_log.render_detail(record, now=1120.0).splitlines() == [
        *box(
            "Routing decision e789d49a · 2m ago",
            "",
            "Prompt preview:  Investigate the service named ai-gateway. Investigate",
            "                 the service named ai-gateway. Investigate the service",
            "                 named ai-gateway.",
            "Selected:        gpt-5-6-terra",
            "Routing time:    4.0s",
            "Rationale:       This task involves several related components. This task",
            "                 involves several related components.",
            "",
            "Candidates",
            "  ○ gpt-6-astra",
            "  ● gpt-5-6-terra  (selected)",
        ),
        "Back to the list: $smart-router logs",
    ]


def test_routing_card_box_is_unchanged():
    assert routing.format_subagent_message("m", "why", subagent_name="s").splitlines() == box(
        "Using Unity Gateway Smart Router - Subagent",
        "Subagent : s",
        "Selected Model : m",
        "Reason : why",
    )


def test_session_logs_are_isolated_from_the_real_home(tmp_path):
    assert session_log.LOG_DIR.is_relative_to(tmp_path)


def _detail_record(**overrides):
    record = {
        "decision_id": "abcdef0123456789",
        "at": 1_000.0,
        "prompt": {"preview": "first line"},
        "original": None,
        "selected_model": "system.ai.gpt-5-6-luna",
        "routing_ms": 1200,
        "rationale": "short reason",
        "candidates": [{"model": "system.ai.gpt-5-6-luna"}],
    }
    return {**record, **overrides}


@pytest.mark.parametrize("at", [10**400, float("inf"), float("nan")])
def test_render_detail_shows_unknown_age_for_out_of_range_timestamps(at):
    assert "Routing decision abcdef01 · ?" in session_log.render_detail(
        _detail_record(at=at), now=2_000.0
    )


def test_render_detail_collapses_whitespace_in_preview_and_rationale():
    out = session_log.render_detail(
        _detail_record(prompt={"preview": "line one\n\tline two"}, rationale="because\nreasons"),
        now=2_000.0,
    )
    assert "�" not in out
    assert "line one line two" in out
    assert "because reasons" in out


def test_render_detail_keeps_the_label_for_an_empty_value():
    out = session_log.render_detail(_detail_record(prompt={"preview": "   "}), now=2_000.0)
    assert "Prompt preview:" in out
