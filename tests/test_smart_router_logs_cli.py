"""Tests for the `ug smart-router-logs` JSON reader."""

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


def write(payload, task="inspect parser", **kwargs):
    session_log.append_decision(
        "claude", payload, task, decision(), "requested", routing_ms=840, **kwargs
    )


def payload(session_id="s1", **extra):
    base = {"session_id": session_id, "tool_use_id": "tool1", "tool_input": {}}
    base.update(extra)
    return base


def read_overviews(session="s1", harness="claude"):
    return json.loads(
        runner.invoke(
            cli.app,
            ["smart-router-logs", "--session", session, "--harness", harness, "--json"],
        ).output
    )["decisions"]


def test_overview_shape_and_order(log_dir):
    write(payload(), task="first task")
    write(payload(), task="second task")
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1", "--json"])
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


def test_private_and_null_preview(log_dir):
    write(payload(), task="secret task", include_task_text=False)
    (overview,) = read_overviews()
    assert overview["preview"] is None


def test_malformed_foreign_and_unknown_type_lines_skipped(log_dir):
    path = log_dir / "claude-s1.jsonl"
    log_dir.mkdir()
    write(payload(), task="kept")
    good = path.read_text()
    path.write_text(
        "not json\n"
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


def test_decision_hit_returns_full_record(log_dir):
    write(payload())
    records = session_log.read_decisions("claude", "s1")
    target = records[0]["decision_id"]
    result = runner.invoke(
        cli.app, ["smart-router-logs", "--session", "s1", "--decision", target, "--json"]
    )
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["v"] == 1
    assert document["decision"] == records[0]


def test_decision_miss_exits_1(log_dir):
    write(payload())
    result = runner.invoke(
        cli.app, ["smart-router-logs", "--session", "s1", "--decision", "nope", "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.output) == {"v": 1, "error": "decision not found"}


def test_json_flag_required(log_dir):
    write(payload())
    result = runner.invoke(cli.app, ["smart-router-logs", "--session", "s1"])
    assert result.exit_code == 2
    assert result.stdout == ""


def test_codex_harness_reads_codex_file(log_dir):
    session_log.append_decision(
        "codex", payload(), "codex task", decision("codex-model"), "requested", routing_ms=5
    )
    assert session_log.session_log_path("claude", "s1").exists() is False
    overviews = read_overviews(harness="codex")
    assert len(overviews) == 1
    assert overviews[0]["selected_model"] == "codex-model"
    assert overviews[0]["preview"] == "codex task"
