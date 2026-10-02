"""Unit checks for interpreting terminal evidence, not live agent substitutes."""

import json

import pytest

from tests.integration.utils import evidence
from tests.integration.utils.agents import _evidence_id, claude, codex
from tests.integration.utils.evidence import (
    SubagentCalculation,
    assert_no_terminal_api_error,
    assistant_answer_contains,
)


class _Session:
    def __init__(self, home):
        self.home = home
        self.artifacts = {}

    def record(self, name, value):
        self.artifacts[name] = value


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


@pytest.mark.parametrize("value", [None, "", " ", 5, {}, [], "model with whitespace"])
def test_completed_task_ids_require_nonempty_strings_without_whitespace(value):
    with pytest.raises(AssertionError, match="Missing or malformed completed-task model"):
        _evidence_id(value, "model")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_completed_task_models_fails_closed_on_missing_model(tmp_path, agent):
    records = completed_records(agent, "model")
    del records[0]["message" if agent == "claude" else "payload"]["model"]
    session = _transcript_session(tmp_path, agent, {"parent.jsonl": records})
    with pytest.raises(AssertionError, match="model"):
        evidence.completed_task_models(session, agent, "value")


@pytest.mark.parametrize("turn_id", ["", "turn with whitespace"])
@pytest.mark.parametrize("record_index", [0, 1])
def test_codex_completed_task_models_fails_closed_on_malformed_turn_ids(turn_id, record_index):
    records = completed_records("codex", "model")
    records[record_index]["payload"]["turn_id"] = turn_id
    with pytest.raises(AssertionError, match="turn ID"):
        codex.completed_task_models(records, "value")


@pytest.mark.parametrize("record_index", [0, 1])
def test_codex_completed_task_models_fails_closed_on_missing_turn_ids(record_index):
    records = completed_records("codex", "model")
    del records[record_index]["payload"]["turn_id"]
    with pytest.raises(AssertionError, match="turn ID"):
        codex.completed_task_models(records, "value")


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
    assert session.artifacts[f"completed-task-model-{agent}-value.json"]["observed"] == sorted(
        models
    )


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_completed_task_model_records_evidence_limits(tmp_path, agent):
    session = _transcript_session(
        tmp_path, agent, {"parent.jsonl": completed_records(agent, "expected")}
    )
    evidence.assert_completed_task_model(session, agent, "value", "expected")
    assert session.artifacts["agent-sessions.json"] == {
        "parent.jsonl": completed_records(agent, "expected")
    }
    assert session.artifacts[f"completed-task-model-{agent}-value.json"] == {
        "expected": "expected",
        "observed": ["expected"],
        "evidence_kind": (
            "response-reported model" if agent == "claude" else "client-selected model"
        ),
        "gateway_destination_proven": False,
    }


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("answer_value", [None, "", " \t", 1, [], {}])
def test_completed_task_models_rejects_empty_or_malformed_expected_answers(agent, answer_value):
    with pytest.raises(AssertionError, match="nonempty answer value"):
        evidence.completed_task_models(None, agent, answer_value)
    adapter = claude.completed_task_models if agent == "claude" else codex.completed_task_models
    with pytest.raises(AssertionError, match="nonempty answer value"):
        adapter([], answer_value)


@pytest.mark.parametrize(
    "message",
    [None, [], {}, {"role": "assistant"}, {"role": "assistant", "content": "value"}],
)
def test_claude_completed_task_models_rejects_malformed_message_metadata(message):
    with pytest.raises(AssertionError):
        claude.completed_task_models([{"type": "assistant", "message": message}], "value")


@pytest.mark.parametrize("content", [[None], [{"type": "text"}], [{"type": "text", "text": 4}]])
def test_claude_completed_task_models_rejects_malformed_answer_blocks(content):
    records = completed_records("claude", "model")
    records[0]["message"]["content"] = content
    with pytest.raises(AssertionError):
        claude.completed_task_models(records, "value")


@pytest.mark.parametrize("payload", [None, [], "value"])
@pytest.mark.parametrize("record_type", ["event_msg", "turn_context"])
def test_codex_completed_task_models_rejects_malformed_payloads(payload, record_type):
    with pytest.raises(AssertionError):
        codex.completed_task_models([{"type": record_type, "payload": payload}], "value")


@pytest.mark.parametrize("answer", [None, [], {}, 7])
def test_codex_completed_task_models_rejects_malformed_completed_answers(answer):
    records = completed_records("codex", "model")
    records[1]["payload"]["last_agent_message"] = answer
    with pytest.raises(AssertionError, match="completed answer"):
        codex.completed_task_models(records, "value")


def test_claude_completed_task_models_normalizes_only_context_window_suffix():
    assert claude.completed_task_models(
        completed_records("claude", "catalog.models.claude_sonnet[1m]"), "value"
    ) == {"catalog.models.claude_sonnet"}


@pytest.mark.parametrize("agent", ["opencode", "unknown"])
def test_completed_task_model_rejects_unsupported_agents(agent):
    session = _Session(None)
    with pytest.raises(AssertionError, match="Unsupported evidence agent"):
        evidence.assert_completed_task_model(session, agent, "value", "model")
