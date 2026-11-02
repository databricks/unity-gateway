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
    decision = routing.RoutingDecision(model="system.ai.claude-opus-4-8")
    routing.write_decision_record(decisions, {"session_id": "s1"}, "x", decision, decision.model)

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
