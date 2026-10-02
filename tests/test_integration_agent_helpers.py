"""Regression checks for extracted native transcript parsing."""

import json
from types import SimpleNamespace

import pytest

from tests.integration.utils import evidence
from tests.integration.utils.agents import claude, codex


@pytest.mark.parametrize("agent,helper", [("claude", claude), ("codex", codex)])
def test_agent_helpers_preserve_native_answers(agent, helper):
    records = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "prompt"}]}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "claude-answer"}, {"type": "tool_use"}],
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "codex-answer"},
        },
    ]
    expected = [f"{agent}-answer"]
    assert helper.assistant_answers(records) == expected
    assert evidence.assistant_answers(agent, records) == expected


@pytest.mark.parametrize("agent,helper", [("claude", claude), ("codex", codex)])
def test_agent_helpers_preserve_session_paths_and_child_detection(tmp_path, agent, helper):
    expected_directory = {"claude": ".claude/projects", "codex": ".codex/sessions"}[agent]
    assert helper.SESSION_DIRECTORY == expected_directory
    directory = tmp_path / expected_directory
    directory.mkdir(parents=True)
    parent = [{"type": "session_meta", "payload": {"source": "cli"}}]
    child = [{"type": "session_meta", "payload": {"source": {"subagent": "spawn"}}}]
    child_path = directory / "project/subagents/child.jsonl"
    child_path.parent.mkdir(parents=True)
    (directory / "parent.jsonl").write_text(json.dumps(parent[0]) + "\n")
    child_path.write_text(json.dumps(child[0]) + "\n")
    session = SimpleNamespace(home=tmp_path)
    assert evidence.agent_sessions(session, agent) == {
        "parent.jsonl": parent,
        "project/subagents/child.jsonl": child,
    }
    assert not helper.is_child_session("parent.jsonl", parent)
    assert not evidence.is_child_session(agent, "parent.jsonl", parent)
    assert helper.is_child_session("project/subagents/child.jsonl", child)
    assert evidence.is_child_session(agent, "project/subagents/child.jsonl", child)
