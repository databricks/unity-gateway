"""Unit checks for interpreting terminal evidence, not live agent substitutes."""

import json

import pytest

from tests.integration.utils import evidence
from tests.integration.utils.agents import claude, codex
from tests.integration.utils.evidence import (
    SubagentCalculation,
    assert_no_terminal_api_error,
    assistant_answer_contains,
    completed_answers,
    completed_child_answers,
    orchestrator_contexts,
    tool_outputs,
)


class _Session:
    def __init__(self, home):
        self.home = home

    def record(self, _name, _value):
        pass


def _transcript_session(home, agent, transcripts):
    directory = home / {"claude": ".claude/projects", "codex": ".codex/sessions"}[agent]
    for name, records in transcripts.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return _Session(home)


def _write_answer(home, agent, *, child, value):
    if agent == "claude":
        directory = home / ".claude/projects/project"
        if child:
            directory /= "subagents"
        record = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": value}]},
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
        '  ⎿ API Error: 403 {"error_code": "PERMISSION_DENIED"}',
        'Review complete.\n\n  ⎿ API Error: 403\n{"error_code": "PERMISSION_DENIED"}',
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


@pytest.mark.parametrize(
    "screen",
    [
        "  3. PERMISSION_DENIED silently swallowed when there's no cached fallback.\n❯",
        "The code raises 'unexpected status 403' when access is denied.",
        "The handler returns 'exceeded retry limit' after repeated errors.",
    ],
)
def test_error_mentions_in_review_text_are_not_terminal_errors(screen):
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


def test_completed_answers_excludes_claude_intermediate_commentary():
    records = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": reason,
                "content": [{"type": "text", "text": answer}],
            },
        }
        for reason, answer in [("tool_use", "I will inspect the module"), ("end_turn", "Reviewed")]
    ]
    assert completed_answers("claude", records) == ["Reviewed"]


def test_completed_child_answers_excludes_inherited_codex_parent_work(tmp_path):
    parent = [
        {"type": "turn_context", "payload": {"turn_id": "parent-turn"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "parent-turn",
                "last_agent_message": "Parent answer",
            },
        },
    ]
    child = [
        {"type": "session_meta", "payload": {"source": {"subagent": "spawn"}}},
        *parent,
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "child-turn",
                "last_agent_message": "Child answer",
            },
        },
    ]
    session = _transcript_session(tmp_path, "codex", {"parent.jsonl": parent, "child.jsonl": child})
    assert completed_child_answers(session, "codex") == {"child.jsonl": ["Child answer"]}


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_orchestrator_context_requires_native_delivery_not_echoed_text(agent):
    context = (
        "Smart Router Orchestrator is on for this session.\n"
        "## Workflow\n### Claude Code adapter\n### Codex adapter"
    )
    records = [
        {"type": "user", "message": {"content": context}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": context}],
            },
        },
    ]
    assert orchestrator_contexts(agent, records) == []
    if agent == "claude":
        records.append(
            {
                "type": "attachment",
                "attachment": {"type": "hook_additional_context", "content": [context]},
            }
        )
    else:
        records.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": context}],
                },
            }
        )
    assert orchestrator_contexts(agent, records) == [context]


def test_codex_model_identity_uses_only_the_completed_answer_turn():
    records = [
        {
            "type": "turn_context",
            "payload": {"turn_id": "other", "model": "catalog.other_models.codex_decoy"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "matching", "model": "catalog.models.gpt_luna"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "matching",
                "last_agent_message": "withheld-file-value",
            },
        },
    ]
    assert codex.completed_task_models(records, "withheld-file-value") == {
        "catalog.models.gpt_luna"
    }


def test_codex_model_identity_rejects_prompt_only_evidence():
    records = [
        {
            "type": "turn_context",
            "payload": {"turn_id": "matching", "model": "catalog.models.gpt_luna"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "withheld-file-value"},
        },
    ]
    assert codex.completed_task_models(records, "withheld-file-value") == set()


def test_claude_model_identity_uses_assistant_answer_not_tool_output():
    records = [
        {
            "type": "user",
            "message": {
                "model": "catalog.other_models.claude_decoy",
                "content": [{"type": "text", "text": "value"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "catalog.models.claude_sonnet",
                "content": [{"type": "text", "text": "value"}],
            },
        },
    ]
    assert claude.completed_task_models(records, "value") == {"catalog.models.claude_sonnet"}


def completed_records(agent, model, answer="value", turn_id="matching"):
    if agent == "claude":
        return [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": answer}],
                },
            }
        ]
    return [
        {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model}},
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": answer},
        },
    ]


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("parent_answer", [None, "value", "unrelated-answer"])
def test_completed_task_models_excludes_child_only_and_conflicting_child_evidence(
    tmp_path, agent, parent_answer
):
    child = completed_records(agent, "child-model")
    if agent == "codex":
        child.insert(0, {"type": "session_meta", "payload": {"source": {"subagent": "spawn"}}})
    sessions = {"project/subagents/child.jsonl": child}
    if parent_answer is not None:
        sessions["project/parent.jsonl"] = completed_records(agent, "parent-model", parent_answer)
    session = _transcript_session(tmp_path, agent, sessions)
    expected = {"parent-model"} if parent_answer == "value" else set()
    assert evidence.completed_task_models(session, agent, "value") == expected


def test_codex_completed_task_models_requires_context_in_same_session(tmp_path):
    context, completion = completed_records("codex", "model")
    session = _transcript_session(
        tmp_path,
        "codex",
        {"context-session.jsonl": [context], "answer-session.jsonl": [completion]},
    )
    with pytest.raises(AssertionError, match="Missing Codex model context"):
        evidence.completed_task_models(session, "codex", "value")


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("models", [[], ["unexpected"], ["expected", "conflicting"]])
def test_completed_task_model_assertion_requires_exact_singleton(tmp_path, agent, models):
    records = [record for model in models for record in completed_records(agent, model)]
    session = _transcript_session(tmp_path, agent, {"parent.jsonl": records})
    with pytest.raises(AssertionError):
        evidence.assert_completed_task_model(session, agent, "value", "expected")


@pytest.mark.parametrize(
    "agent,record",
    [
        (
            "claude",
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "confirmed"}]},
            },
        ),
        (
            "claude",
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": "confirmed"}],
                        }
                    ]
                },
            },
        ),
        (
            "codex",
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "confirmed"},
            },
        ),
        (
            "codex",
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": [{"type": "input_text", "text": "confirmed"}],
                },
            },
        ),
    ],
)
def test_tool_outputs_read_native_string_and_block_results(agent, record):
    assert tool_outputs(agent, [record]) == ["confirmed"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_tool_outputs_exclude_user_echoes_and_assistant_claims(agent):
    records = [
        {"type": "user", "message": {"role": "user", "content": "confirmed"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "confirmed"}]},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "confirmed"}],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": "confirmed", "is_error": True}]
            },
        },
    ]
    assert tool_outputs(agent, records) == []
