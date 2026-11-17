"""Tests for the `ug smart-router-logs` JSON reader."""

import hashlib
import json
import uuid

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.smart_routing import routing, session_log

runner = CliRunner()


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setattr(session_log, "LOG_DIR", directory)
    return directory


def decision(model="selected"):
    return routing.RoutingDecision(model, "reason", ((model, "claude"), ("other", None)), "stable")


def write(payload, task="inspect parser"):
    session_log.append_decision("claude", payload, task, decision(), "requested", routing_ms=840)


def payload(session_id="s1", **extra):
    base = {"session_id": session_id, "tool_use_id": "tool1", "tool_input": {}}
    base.update(extra)
    return base


def read_overviews(session="s1"):
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", session])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["decisions"]


def test_overview_shape_and_order(log_dir):
    write(payload(), task="first task")
    write(payload(), task="second task")
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["v"] == 1
    overviews = document["decisions"]
    assert [o["preview"] for o in overviews] == ["first task", "second task"]
    assert set(overviews[0]) == {
        "decision_id",
        "at",
        "kind",
        "preview",
        "selected_model",
        "routing_ms",
    }
    assert overviews[0]["kind"] == "subagent"
    assert overviews[0]["selected_model"] == "selected"
    assert overviews[0]["routing_ms"] == 840
    assert uuid.UUID(hex=overviews[0]["decision_id"]).version == 4
    assert overviews[0]["at"] <= overviews[1]["at"]


def test_empty_task_has_null_preview(log_dir):
    write(payload(), task="")
    (overview,) = read_overviews()
    assert overview["preview"] is None


def test_malformed_foreign_and_unknown_type_lines_skipped(log_dir):
    path = log_dir / "claude-s1.jsonl"
    log_dir.mkdir()
    write(payload(), task="kept")
    good = path.read_text()
    path.write_text(
        "not json\n"
        + "[" * 10_000
        + "]" * 10_000
        + "\n"
        + json.dumps({"v": 1})
        + "\n"
        + json.dumps({"v": 2, "type": "decision", "decision_id": "newer"})
        + "\n"
        + json.dumps({"v": 1, "type": "other", "decision_id": "future"})
        + "\n"
        + json.dumps([1, 2, 3])
        + "\n"
        + "\n"
        + good
    )
    overviews = read_overviews()
    assert len(overviews) == 1
    assert overviews[0]["preview"] == "kept"


def test_missing_session_returns_empty_list(log_dir):
    assert read_overviews(session="absent") == []


def test_unreadable_log_returns_error_envelope(log_dir, monkeypatch):
    """A log that exists but cannot be read must not be reported as empty history."""

    def deny_read(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    write(payload())
    path = log_dir / "claude-s1.jsonl"
    monkeypatch.setattr(type(path), "read_text", deny_read)
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert result.exit_code == 1
    document = json.loads(result.output)
    assert document["v"] == 1
    assert "cannot read session log at" in document["error"]
    assert "claude-s1.jsonl" in document["error"]


def test_decision_hit_returns_full_record(log_dir):
    write(payload())
    record = json.loads((log_dir / "claude-s1.jsonl").read_text())
    result = runner.invoke(
        cli.app, ["smart-router-logs", "--session", "s1", "--decision", record["decision_id"]]
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["v"] == 1
    assert document["decision"] == record
    assert record["routing_ms"] == 840
    assert record["requested_model"] == "requested"
    assert record["prompt"] == {
        "preview": "inspect parser",
        "sha256": hashlib.sha256(b"inspect parser").hexdigest(),
        "length": len("inspect parser"),
    }


def test_decision_miss_exits_1(log_dir):
    write(payload())
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--decision", "nope"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"v": 1, "error": "decision not found"}


def write_codex(session_id="c1", task="inspect parser", model="system.ai.databricks-gpt-5-6-sol"):
    session_log.append_decision(
        "codex", payload(session_id), task, decision(model), "requested", routing_ms=1234
    )


def table(*args):
    return runner.invoke(
        cli.app,
        ["smart-router-logs", "--session", "c1", "--harness", "codex", "--format", "table", *args],
    )


def test_harness_defaults_to_claude(log_dir):
    write_codex()
    assert read_overviews(session="c1") == []


def test_codex_harness_reads_codex_log_as_json(log_dir):
    write_codex()
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "c1", "--harness", "codex"])
    assert result.exit_code == 0
    assert [o["preview"] for o in json.loads(result.output)["decisions"]] == ["inspect parser"]


def test_table_shows_header_and_row_with_stripped_model_prefix(log_dir):
    write_codex()
    result = table()
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "1 routed subagents this session (showing last 1)"
    assert lines[2].split() == ["ID", "WHEN", "TASK", "MODEL", "ROUTING"]
    assert lines[3].split(maxsplit=3)[1:3] == ["just", "now"]
    assert "inspect parser" in lines[3]
    assert "gpt-5-6-sol" in lines[3]
    assert "system.ai" not in lines[3]
    assert "1234 ms" in lines[3]


def test_table_caps_rows_and_truncates_task(log_dir):
    for index in range(25):
        write_codex(task=f"task {index} " + "x" * 200)
    result = table()
    lines = result.output.splitlines()
    assert lines[0] == "25 routed subagents this session (showing last 20)"
    assert len(lines) == 3 + 20 + 2
    assert "task 5 " in lines[3]
    assert "task 4 " not in result.output
    assert all("…" in line for line in lines[3:23])
    assert max(len(line) for line in lines[3:23]) < 130


def test_table_empty_message(log_dir):
    result = table()
    assert result.exit_code == 0
    assert result.output == "No routed subagents recorded this session yet.\n"


def test_table_error_is_one_plain_line(log_dir, monkeypatch):
    def deny_read(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    write_codex()
    monkeypatch.setattr(type(log_dir), "read_text", deny_read)
    result = table()
    assert result.exit_code == 1
    assert result.output.startswith("error: cannot read session log at ")
    assert len(result.output.splitlines()) == 1


def test_unknown_harness_rejected(log_dir):
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--harness", "gemini"])
    assert result.exit_code == 1
    assert "unknown harness" in json.loads(result.output)["error"]


def test_table_has_id_column_and_hint(log_dir):
    write_codex()
    decision_id = json.loads((log_dir / "codex-c1.jsonl").read_text())["decision_id"]
    lines = table().output.splitlines()
    assert lines[3].split()[0] == decision_id[:8]
    assert lines[-1] == "Show one: $smart-router logs --id <ID>"


def detail(query):
    return table("--decision", query)


def test_detail_renders_all_fields_and_marks_selected(log_dir):
    session_log.append_decision(
        "codex",
        payload("c1", tool_input={"subagent_type": "explorer"}),
        "inspect parser",
        routing.RoutingDecision(
            "system.ai.databricks-gpt-5-6-sol",
            "Needs depth. " * 20,
            (("system.ai.databricks-gpt-5-6-sol", "codex"), ("system.ai.gpt-5-6-luna", "codex")),
            "stable",
        ),
        "requested",
        routing_ms=1234,
    )
    record = json.loads((log_dir / "codex-c1.jsonl").read_text())
    result = detail(record["decision_id"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "Prompt preview: inspect parser"
    assert "Agent type: explorer" in lines
    assert "Selected: gpt-5-6-sol" in lines
    assert "Routing time: 1.2s" in lines
    assert max(len(line) for line in lines) <= 90
    assert lines[-2:] == ["● gpt-5-6-sol", "○ gpt-5-6-luna"]


def test_detail_handles_missing_rationale_candidates_and_route_agent(log_dir):
    log_dir.mkdir()
    record = {
        "v": 1,
        "type": "decision",
        "decision_id": "abcd1234",
        "prompt": None,
        "original": {"subagent_type": "ug-smart-router:ucode-route-x"},
        "selected_model": "m",
        "routing_ms": 50,
        "rationale": "  ",
        "candidates": None,
    }
    (log_dir / "codex-c1.jsonl").write_text(json.dumps(record) + "\n")
    output = detail("abcd1234").output
    assert "(prompt not recorded)" in output
    assert "Agent type" not in output
    assert "Routing time: 0.1s" in output
    assert "No rationale returned." in output
    assert output.endswith("Candidates:\n")


def test_detail_prefix_matching(log_dir):
    log_dir.mkdir()
    ids = ["abcd1111" + "0" * 24, "abcd2222" + "0" * 24, "ffff3333" + "0" * 24]
    (log_dir / "codex-c1.jsonl").write_text(
        "".join(
            json.dumps({"v": 1, "type": "decision", "decision_id": i, "selected_model": "m"}) + "\n"
            for i in ids
        )
    )
    assert detail(ids[0]).exit_code == 0
    assert detail("ffff").exit_code == 0
    ambiguous = detail("abcd")
    assert ambiguous.exit_code == 1 and "ambiguous" in ambiguous.output
    short = detail("ab")
    assert short.exit_code == 1 and "at least 4" in short.output
    missing = detail("eeee")
    assert missing.exit_code == 1 and missing.output == "error: decision not found\n"
    assert len(missing.output.splitlines()) == 1


def test_json_decision_still_requires_exact_id(log_dir):
    write_codex()
    decision_id = json.loads((log_dir / "codex-c1.jsonl").read_text())["decision_id"]
    result = runner.invoke(
        cli.app,
        [
            "smart-router-logs",
            "--session",
            "c1",
            "--harness",
            "codex",
            "--decision",
            decision_id[:8],
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"v": 1, "error": "decision not found"}
