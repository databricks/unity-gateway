"""Unit checks for interpreting terminal evidence, not live agent substitutes."""

import json

import pytest

from tests.integration.utils import evidence
from tests.integration.utils.agents import antigravity, claude, codex, copilot
from tests.integration.utils.evidence import (
    FileTask,
    SubagentCalculation,
    assert_no_terminal_api_error,
    assistant_answer_contains,
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


@pytest.mark.parametrize("reason", ["RESOURCE_EXHAUSTED", "REQUEST_LIMIT_EXCEEDED"])
def test_antigravity_resource_exhausted_detects_provider_429(reason):
    stdout = json.dumps(
        {
            "event": "result",
            "result": {
                "status": "ERROR",
                "error": f"request failed (code 429): {reason}",
            },
        }
    )

    assert antigravity.resource_exhausted(stdout)


@pytest.mark.parametrize(
    "result",
    [
        {"status": "SUCCESS", "response": "RESOURCE_EXHAUSTED code 429"},
        {"status": "ERROR", "error": "request failed (code 500): RESOURCE_EXHAUSTED"},
        {"status": "ERROR", "error": "request failed (code 429): PERMISSION_DENIED"},
    ],
)
def test_antigravity_resource_exhausted_rejects_non_retryable_results(result):
    stdout = json.dumps({"event": "result", "result": result})

    assert not antigravity.resource_exhausted(stdout)


def test_antigravity_resource_exhausted_retry_delay_is_truncated_exponential(monkeypatch):
    monkeypatch.setattr(antigravity.random, "uniform", lambda _start, _end: 0)

    assert [antigravity.resource_exhausted_retry_delay(index) for index in range(5)] == [
        5,
        10,
        20,
        20,
        20,
    ]


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


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("child", [False, True])
def test_file_task_child_completion_is_independent_of_parent_answer(tmp_path, agent, child):
    session = _Session(tmp_path)
    session.cwd = tmp_path
    task = FileTask(session)
    assert task.value not in task.delegate_prompt
    assert not task.completed(session, agent, child=True)
    _write_answer(tmp_path, agent, child=child, value=task.value)
    assert task.completed(session, agent, child=True) == child
    assert task.completed(session, agent) != child


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


def test_copilot_model_identity_uses_only_answering_assistant_messages():
    records = [
        {"type": "user.message", "data": {"content": "withheld-file-value"}},
        {
            "type": "assistant.message",
            "data": {"content": "Reading the file.", "model": "system.ai.claude-haiku-4-5"},
        },
        {
            "type": "assistant.message",
            "data": {"content": "withheld-file-value", "model": "system.ai.gpt-5-4-nano"},
        },
        {"type": "assistant.message"},
        {"type": "assistant.message", "data": {"content": None, "model": "system.ai.gpt-5"}},
    ]
    assert copilot.completed_task_models(records, "withheld-file-value") == {
        "system.ai.gpt-5-4-nano"
    }
    assert copilot.assistant_answers(records) == ["Reading the file.", "withheld-file-value"]


def test_copilot_model_identity_rejects_an_answer_without_a_model():
    records = [{"type": "assistant.message", "data": {"content": "withheld-file-value"}}]
    with pytest.raises(AssertionError, match="completed-task model"):
        copilot.completed_task_models(records, "withheld-file-value")


def test_copilot_selected_models_follow_start_then_switches():
    records = [
        {"type": "session.start", "data": {"selectedModel": "dbx-anthropic/claude"}},
        {"type": "session.model_change", "data": {"newModel": "dbx-responses/gpt"}},
        {"type": "session.model_change", "data": {}},
        {"type": "assistant.message", "data": {"model": "system.ai.gpt"}},
    ]
    assert copilot.selected_models(records) == ["dbx-anthropic/claude", "dbx-responses/gpt"]


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


def test_claude_assistant_answers_include_subagent_handback():
    records = [
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-use",
                        "name": "SubagentHandback",
                        "input": {"message": "child answer"},
                    }
                ],
            },
        }
    ]

    assert claude.assistant_answers(records) == ["child answer"]


def test_claude_assistant_answers_ignore_parent_handback():
    records = [
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-use",
                        "name": "SubagentHandback",
                        "input": {"message": "not a child answer"},
                    }
                ],
            },
        }
    ]

    assert claude.assistant_answers(records) == []


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


@pytest.mark.parametrize("orchestrator_on", [False, True])
def test_codex_generic_routing_label_correlates_to_tagged_prompt(orchestrator_on):
    suffix = " [orchestrator on]" if orchestrator_on else ""
    panel = (
        "│ Using Unity Gateway Smart Router - Subagent │\n"
        f"│ Subagent : subagent{suffix} │\n"
        "│ Selected Model : gpt-6-luna │\n└──────────┘\n"
    )
    prompt = '› Spawn a child with task name "ug_subagent_ab12"\n  and return its answer.\n\n'
    screen = prompt + panel + "› Ask Codex to do anything\n"
    assert evidence.codex_routing_display_for_task(
        screen, "ug_subagent_ab12", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        screen, "ug_subagent_other", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        panel + prompt, "ug_subagent_ab12", orchestrator_on=orchestrator_on
    )
    assert not evidence.codex_routing_display_for_task(
        prompt + "› Another task\n\n" + panel,
        "ug_subagent_ab12",
        orchestrator_on=orchestrator_on,
    )
    assert not evidence.codex_routing_display_for_task(
        screen, "ug_subagent_ab12", orchestrator_on=not orchestrator_on
    )


def test_codex_compacted_prompt_uses_adjacent_native_spawn():
    panel = (
        "• Context compacted\n\n"
        "│ Using Unity Gateway Smart Router - Subagent │\n"
        "│ Subagent : subagent [orchestrator on] │\n"
        "│ Selected Model : gpt-6-luna │\n└──────────┘\n\n"
    )
    spawn = "• Spawned child-id (gpt-6-luna)\n  └ Task name: ug_subagent_ab12. Calculate 2+2.\n\n"
    assert evidence.codex_routing_display_for_task(
        panel + spawn, "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + spawn, "ug_subagent_other", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "• Answer: ug_subagent_ab12=4\n", "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "› New prompt\n\n" + spawn, "ug_subagent_ab12", orchestrator_on=True
    )
    assert not evidence.codex_routing_display_for_task(
        panel + "• Spawned other-child\n  └ Another task\n\n" + spawn,
        "ug_subagent_ab12",
        orchestrator_on=True,
    )
