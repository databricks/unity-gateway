"""Tests for Claude Code smart-routing helpers."""

from __future__ import annotations

import json

import pytest

from ucode.smart_routing import claude_routing, routing


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [("Agent", True), ("Task", True), (" task ", True), ("Bash", False), (None, False)],
)
def test_spawn_tool_detector_matches_agent_and_legacy_task(tool_name, expected):
    assert claude_routing.is_spawn_agent_tool(tool_name) is expected


def test_canary_and_audit_are_written(tmp_path, monkeypatch):
    canary = tmp_path / "canary.json"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(claude_routing, "CANARY_PATH", canary)
    monkeypatch.setattr(claude_routing, "AUDIT_PATH", audit)

    claude_routing.record_session_start({"session_id": "s1", "model": "system.ai.claude-opus-4-8"})
    claude_routing.record_subagent_start(
        {
            "session_id": "s1",
            "agent_id": "a1",
            "agent_type": "Explore",
            "model": "system.ai.claude-sonnet-5",
        }
    )

    assert json.loads(canary.read_text())["session_id"] == "s1"
    assert json.loads(audit.read_text().strip())["agent_id"] == "a1"


def test_decision_is_reconciled_with_actual_subagent_model(tmp_path, monkeypatch):
    decisions = tmp_path / "decisions.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(claude_routing, "DECISIONS_PATH", decisions)
    monkeypatch.setattr(claude_routing, "AUDIT_PATH", audit)
    task = "Review this code: " + "x" * 10000
    decision = routing.RoutingDecision(
        model="system.ai.claude-opus-4-8", rationale="why " * 1000, raw_rationale="raw " * 1000
    )
    routing.write_decision_record(decisions, {"session_id": "s1"}, task, decision, decision.model)
    saved = json.loads(decisions.read_text())
    assert saved["task_name"] == task
    assert saved["rationale"] == decision.rationale
    assert saved["raw_rationale"] == decision.raw_rationale

    record = claude_routing.record_subagent_start(
        {"session_id": "s1", "agent_id": "a1", "model": "system.ai.claude-opus-4-8"}
    )

    assert record["router_model"] == "system.ai.claude-opus-4-8"
    assert record["requested_model"] == "system.ai.claude-opus-4-8"
    assert record["matches_router_decision"] is True


def test_subagent_start_without_model_reports_unknown_not_mismatch(tmp_path, monkeypatch):
    # Claude Code's SubagentStart event does not include the subagent's model,
    # so reconciliation can't verify the match — record None (unknown) rather
    # than a false mismatch. The PreToolUse hook already injected the model.
    decisions = tmp_path / "decisions.jsonl"
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(claude_routing, "DECISIONS_PATH", decisions)
    monkeypatch.setattr(claude_routing, "AUDIT_PATH", audit)
    decision = routing.RoutingDecision(model="system.ai.claude-sonnet-5")
    routing.write_decision_record(decisions, {"session_id": "s2"}, "x", decision, decision.model)

    record = claude_routing.record_subagent_start(
        {"session_id": "s2", "agent_id": "a2", "model": None}
    )

    assert record["requested_model"] == "system.ai.claude-sonnet-5"
    assert record["matches_router_decision"] is None


@pytest.mark.parametrize(("actual", "expected"), [("second", "d2"), ("absent", "d1"), (None, "d1")])
def test_journal_matching_preserves_first_pending_and_skips_used_entries(
    tmp_path, actual, expected
):
    decisions, audit = tmp_path / "decisions.jsonl", tmp_path / "audit.jsonl"
    rows = [
        {"session_id": "s", "decision_id": "used", "requested_model": actual},
        {"session_id": "other", "decision_id": "foreign", "requested_model": actual},
        {"session_id": "s", "decision_id": "d1", "requested_model": "first"},
        {"session_id": "s", "decision_id": "d2", "requested_model": "second"},
        {"session_id": "s", "decision_id": "d3", "requested_model": "second"},
    ]
    decisions.write_text("broken\nnull\n" + "\n".join(map(json.dumps, rows)), encoding="utf-8")
    audit.write_text('[]\n{"decision_id":"used"}\nbroken', encoding="utf-8")
    assert routing._pending_decision(decisions, audit, "s", actual)["decision_id"] == expected


def test_matching_decision_does_not_decode_the_remaining_journal(tmp_path, monkeypatch):
    decisions, audit = tmp_path / "decisions.jsonl", tmp_path / "missing-audit.jsonl"
    target = {"session_id": "s", "decision_id": "match", "requested_model": "model"}
    decisions.write_text(json.dumps(target) + "\n" + "{}\n" * 1000, encoding="utf-8")
    loads = json.loads
    decoded = []

    def counted(line):
        decoded.append(line)
        return loads(line)

    monkeypatch.setattr(routing.json, "loads", counted)
    assert routing._pending_decision(decisions, audit, "s", "model") == target
    assert len(decoded) == 1
