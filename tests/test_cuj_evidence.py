"""Offline negative controls for the live CUJ's native-session evidence reader."""

import copy
import json
from types import SimpleNamespace

import pytest

from tests.e2e_cuj.helpers.constants import CLAUDE, CODEX
from tests.e2e_cuj.helpers.evidence import (
    BaseCujHelper,
    ClaudeCujHelper,
    CodexCujHelper,
    SessionEvidence,
    canonical_model,
    completed_turn,
    get_cuj_helper,
)
from tests.integration.utils.evidence import FileTask, read_jsonl


def records(agent, task, model):
    if agent == CLAUDE:
        return [
            {"type": "user", "sessionId": "session", "message": {"content": task.prompt}},
            {
                "type": "assistant",
                "sessionId": "session",
                "message": {
                    "id": "response",
                    "model": model,
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": task.value}],
                },
            },
        ]
    elif agent == CODEX:
        return [
            {"type": "session_meta", "payload": {"id": "session", "source": "cli"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": model}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": task.prompt}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn",
                    "last_agent_message": task.value,
                },
            },
        ]
    else:
        raise ValueError(f"Unsupported agent: {agent!r}")


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("agent", ["", "claud", "unsupported"])
def test_cuj_evidence_rejects_unknown_agents(tmp_path, agent):
    task = SimpleNamespace(prompt="prompt", value="answer")
    with pytest.raises(ValueError, match="Unsupported agent"):
        records(agent, task, "model")
    with pytest.raises(ValueError, match="Unsupported agent"):
        completed_turn(agent, [], task)
    with pytest.raises(ValueError, match="Unsupported agent"):
        SessionEvidence(tmp_path, agent)
    with pytest.raises(ValueError, match="Unsupported agent"):
        get_cuj_helper(agent)


@pytest.mark.parametrize("agent, helper", [(CLAUDE, ClaudeCujHelper), (CODEX, CodexCujHelper)])
def test_cuj_evidence_dispatches_to_agent_helper(tmp_path, agent, helper):
    task = SimpleNamespace(prompt="prompt", value="answer")
    native = records(agent, task, "model")
    assert issubclass(helper, BaseCujHelper)
    assert get_cuj_helper(agent) is helper
    assert completed_turn(agent, native, task) == helper._completed_turn(native, task)
    boundary = SessionEvidence(tmp_path, agent)
    assert boundary.helper is helper
    assert boundary.directory == tmp_path / helper.session_directory


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
def test_cuj_evidence_completed_native_turn(tmp_path, agent):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    model = {CLAUDE: "system.ai.claude-opus-4-8", CODEX: "system.ai.gpt-6-luna"}[agent]
    boundary = SessionEvidence(tmp_path, agent)
    write_rows(boundary.directory / "new.jsonl", records(agent, task, model))
    assert task.value not in task.prompt
    result = boundary.assert_applied(task, {model}, expected=model)
    assert result["selected_model"] == model


def test_cuj_evidence_claude_async_notification_keeps_parent_turn():
    task = SimpleNamespace(
        prompt="Delegate reading the file to one subagent.", value="hidden-value"
    )
    prompt, answer = records(CLAUDE, task, "system.ai.claude-haiku-4-5")
    delegation = copy.deepcopy(answer)
    delegation["message"].update(
        id="delegation",
        stop_reason="tool_use",
        content=[
            {"type": "tool_use", "id": "tool", "name": "Agent", "input": {"prompt": task.prompt}}
        ],
    )
    waiting = copy.deepcopy(answer)
    waiting["message"].update(
        id="waiting", content=[{"type": "text", "text": "Waiting for the child."}]
    )
    rows = [
        prompt,
        delegation,
        {
            "type": "user",
            "sessionId": "session",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool", "content": task.value}]
            },
        },
        waiting,
        {
            "type": "user",
            "sessionId": "session",
            "origin": {"kind": "task-notification"},
            "promptSource": "system",
            "turnOrigin": "task_notification",
            "message": {
                "content": (
                    "<task-notification>\n<status>completed</status>\n"
                    f"<result>{task.value}</result>\n</task-notification>"
                )
            },
        },
        answer,
    ]
    assert completed_turn(CLAUDE, rows[:3], task) is None
    assert completed_turn(CLAUDE, rows[:-1], task) is None
    turn = completed_turn(CLAUDE, rows, task)
    assert turn is not None
    assert (turn.session_id, turn.turn_id, turn.answer) == ("session", "response", task.value)
    assert turn.models == ["system.ai.claude-haiku-4-5"] * 3
    child = copy.deepcopy(rows)
    child[-1]["isSidechain"] = True
    assert completed_turn(CLAUDE, child, task) is None
    with pytest.raises(AssertionError, match="Prompt was submitted more than once"):
        completed_turn(CLAUDE, [*rows, prompt], task)
    missing_model = copy.deepcopy(rows)
    del missing_model[3]["message"]["model"]
    with pytest.raises(AssertionError, match="Missing inference model metadata"):
        completed_turn(CLAUDE, missing_model, task)


def test_cuj_evidence_claude_real_user_message_ends_parent_turn():
    task = SimpleNamespace(
        prompt="Delegate reading the file to one subagent.", value="hidden-value"
    )
    prompt, answer = records(CLAUDE, task, "system.ai.claude-haiku-4-5")
    user = {
        "type": "user",
        "sessionId": "session",
        "origin": {"kind": "human"},
        "message": {"content": "A different task."},
    }
    assert completed_turn(CLAUDE, [prompt, user, answer], task) is None
    user["message"]["content"] = (
        f"<task-notification><result>{task.value}</result></task-notification>"
    )
    assert completed_turn(CLAUDE, [prompt, user, answer], task) is None


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
@pytest.mark.parametrize(
    "failure",
    [
        "unsupported",
        "no_prompt",
        "incomplete",
        "child",
    ],
)
def test_cuj_evidence_rejects_false_positives(tmp_path, agent, failure):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    model = {CLAUDE: "system.ai.claude-opus-4-8", CODEX: "system.ai.gpt-6-sol"}[agent]
    boundary = SessionEvidence(tmp_path, agent)
    rows = records(agent, task, model)
    if failure == "no_prompt":
        rows = [
            row
            for row in rows
            if row.get("type") != "user"
            and row.get("payload", {}).get("type") not in {"user_message", "message"}
        ]
    if failure == "incomplete":
        rows.pop()
    if failure == "child":
        if agent == CLAUDE:
            rows[0]["isSidechain"] = True
        elif agent == CODEX:
            rows[0]["payload"]["source"] = {"subagent": "parent"}
        else:
            raise ValueError(f"Unsupported agent: {agent!r}")
    write_rows(boundary.directory / "new.jsonl", rows)
    with pytest.raises(AssertionError):
        boundary.assert_applied(
            task,
            {"system.ai.other"} if failure == "unsupported" else {model},
            expected=model,
        )


def test_cuj_evidence_codex_rejects_wrong_turn(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    rows = records(CODEX, task, "gpt-6-sol")
    wrong_turn = copy.deepcopy(rows)
    wrong_turn[-1]["payload"]["turn_id"] = "different-turn"
    assert completed_turn(CODEX, wrong_turn, task) is None


def test_cuj_evidence_ignores_existing_session(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    first = SessionEvidence(tmp_path, CLAUDE)
    write_rows(first.directory / "old.jsonl", records(CLAUDE, task, "system.ai.claude-opus-4-8"))
    boundary = SessionEvidence(tmp_path, CLAUDE)
    assert boundary.completed(task) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("anthropic-aigw-1234abcd-system.ai.glm-5-2[1m]", "system.ai.glm-5-2"),
        ("gpt-5.6-sol", "system.ai.gpt-5-6-sol"),
        ("system.ai.gpt-6-sol", "system.ai.gpt-6-sol"),
        (
            "anthropic-aigw-invalid-claude-opus-4-8",
            "system.ai.anthropic-aigw-invalid-claude-opus-4-8",
        ),
    ],
)
def test_cuj_evidence_only_normalizes_known_aliases(raw, expected):
    assert canonical_model(raw) == expected


def test_cuj_evidence_shared_jsonl_reader_handles_partial_and_complete_final_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"a": 1}\n{"partial":')
    assert read_jsonl(path) == [{"a": 1}]
    path.write_text('{"a": 1}\n{"b": 2}')
    assert read_jsonl(path) == [{"a": 1}, {"b": 2}]
    path.write_text("not json\n")
    with pytest.raises(json.JSONDecodeError):
        read_jsonl(path)


def test_cuj_uses_shared_file_task_without_exposing_answer(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    task.prompt += " Do not delegate."
    assert (tmp_path / task.filename).read_text().strip() == task.value
    assert task.value not in task.prompt
    assert "Do not delegate." in task.prompt
