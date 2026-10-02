"""Unit checks for interpreting terminal evidence, not live agent substitutes."""

import json

import pytest

from tests.integration.utils.evidence import (
    SubagentCalculation,
    assert_no_terminal_api_error,
    assistant_answer_contains,
)


class _Session:
    def __init__(self, home):
        self.home = home

    def record(self, _name, _value):
        pass


def _write_answer(home, agent, *, child, value, handback=False):
    if agent == "claude":
        directory = home / ".claude/projects/project"
        if child:
            directory /= "subagents"
        content = (
            [{"type": "tool_use", "name": "SubagentHandback", "input": {"message": value}}]
            if handback
            else [{"type": "text", "text": value}]
        )
        record = {
            "type": "assistant",
            "message": {"role": "assistant", "content": content},
        }
    else:
        directory = home / ".codex/sessions"
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        records = [
            {"type": "session_meta", "payload": {"source": source if child else "cli"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": value},
            },
        ]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ("child.jsonl" if child else "parent.jsonl")).write_text(
            "".join(json.dumps(row) + "\n" for row in records)
        )
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("child.jsonl" if child else "parent.jsonl")).write_text(json.dumps(record) + "\n")


@pytest.mark.parametrize(
    "screen",
    [
        "■ exceeded retry limit, last status: 429 Too Many Requests",
        "■ unexpected status 403 Forbidden: PERMISSION_DENIED",
        "■ unexpected status 401 Unauthorized",
    ],
)
def test_terminal_api_failure_reports_the_actual_error(screen):
    with pytest.raises(AssertionError, match="Agent returned a terminal API error") as error:
        assert_no_terminal_api_error(screen)
    assert screen in str(error.value)


@pytest.mark.parametrize(
    "screen",
    [
        "Reconnecting... 1/5 (unexpected status 429 Too Many Requests)",
        "Reconnecting... 1/5 (unexpected status 503 Service Unavailable)",
        "Working (5s · esc to interrupt)",
    ],
)
def test_transient_retries_and_running_tasks_are_not_terminal_errors(screen):
    assert_no_terminal_api_error(screen)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_tagged_calculation_requires_the_native_child_answer(tmp_path, agent):
    session = _Session(tmp_path)
    task = SubagentCalculation("1+1", "2")
    _write_answer(tmp_path, agent, child=False, value=task.value)

    assert task.completed(session, agent)
    assert not task.completed(session, agent, child=True)

    _write_answer(tmp_path, agent, child=True, value=task.value)

    assert task.completed(session, agent, child=True)
    assert assistant_answer_contains(session, agent, task.value, child=True)
    assert task.marker in task.prompt
    assert f'task name "{task.marker}"' in task.prompt
    assert "1+1" in task.prompt


def test_claude_subagent_handback_is_the_native_child_answer(tmp_path):
    session = _Session(tmp_path)
    task = SubagentCalculation("1+1", "2")
    _write_answer(tmp_path, "claude", child=True, value=task.value, handback=True)

    assert task.completed(session, "claude", child=True)


def test_claude_ordinary_tool_input_is_not_an_answer(tmp_path):
    session = _Session(tmp_path)
    task = SubagentCalculation("1+1", "2")
    directory = tmp_path / ".claude/projects/project/subagents"
    directory.mkdir(parents=True)
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Agent",
                    "input": {"prompt": task.value},
                }
            ],
        },
    }
    (directory / "child.jsonl").write_text(json.dumps(record) + "\n")

    assert not task.completed(session, "claude", child=True)
