"""Offline negative controls for the live CUJ's native-session evidence reader."""

import copy
import json
from types import SimpleNamespace

import pytest

from tests.e2e_cuj.helpers.constants import CLAUDE, CODEX, INFERENCE_PATHS
from tests.e2e_cuj.helpers.evidence import (
    BaseCujHelper,
    ClaudeCujHelper,
    CodexCujHelper,
    SessionEvidence,
    assert_claude_headless_model,
    assert_inference_evidence,
    canonical_model,
    completed_turn,
    get_cuj_helper,
)
from tests.e2e_cuj.helpers.tui_request_recorder import RecordedRequest, RecordedResponse
from tests.e2e_cuj.test_cuj4_smart_routing import _task_inference_request
from tests.integration.utils.evidence import FileTask, read_jsonl
from tests.integration.utils.provider_catalog import MODEL_SERVICE_PARENT_SCHEMA_HEADER


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


def _tagged_codex_notification(row):
    notification = copy.deepcopy(row)
    notification["payload"]["content"] = [{"type": "input_text", "text": "<subagent_notification>"}]
    notification["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": ["multi_agent.subagent_notification"]
    }
    return notification


def _codex_parent_delegation_records(task, model):
    rows = records(CODEX, SimpleNamespace(prompt="initial", value="initial"), model)
    target = records(CODEX, task, model)[1:]
    for row in target:
        if row["type"] != "response_item":
            row["payload"]["turn_id"] = "delegate"
    rows.extend(
        [
            {"type": "event_msg", "payload": {"type": "user_message", "message": task.prompt}},
            *target[:3],
            _tagged_codex_notification(target[2]),
            target[3],
        ]
    )
    return rows


def test_cuj_evidence_codex_delegated_parent_turn(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    task.prompt = task.delegate_prompt
    rows = _codex_parent_delegation_records(task, "gpt-6-sol")
    later = records(CODEX, SimpleNamespace(prompt="later", value="later"), "gpt-6-sol")[1:]
    for row in later:
        if row["type"] != "response_item":
            row["payload"]["turn_id"] = "later"
    rows.extend(later)
    turn = completed_turn(CODEX, rows, task)
    assert turn is not None
    assert (turn.turn_id, turn.answer) == ("delegate", task.value)


@pytest.mark.parametrize("case", ["aborted", "later", "notification", "duplicate"])
def test_cuj_evidence_codex_rejects_follow_up_false_positives(tmp_path, case):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    rows = records(CODEX, task, "gpt-6-sol")
    if case == "aborted":
        rows[-1]["payload"]["type"] = "turn_aborted"
    elif case == "later":
        rows.pop()
        rows.append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}})
    elif case == "notification":
        rows[3] = _tagged_codex_notification(rows[3])
    else:
        rows.insert(-1, copy.deepcopy(rows[3]))
    if case == "duplicate":
        with pytest.raises(AssertionError, match="Prompt was submitted more than once"):
            completed_turn(CODEX, rows, task)
    else:
        assert completed_turn(CODEX, rows, task) is None


def _inference_request(agent, sequence, prompt, tools):
    field = "messages" if agent == CLAUDE else "input"
    content = prompt if agent == CLAUDE else [{"type": "input_text", "text": prompt}]
    return SimpleNamespace(
        method="POST",
        path=INFERENCE_PATHS[agent],
        sequence=sequence,
        payload={field: [{"role": "user", "content": content}], "tools": tools},
    )


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
@pytest.mark.parametrize(
    ("method", "path", "sequence"),
    [("GET", None, 2), ("POST", "/models", 2), ("POST", None, 1)],
)
def test_cuj_task_inference_skips_unrelated_empty_bodies(agent, method, path, sequence):
    unrelated = RecordedRequest(sequence, method, path or INFERENCE_PATHS[agent], {}, b"")
    task = _inference_request(agent, 3, "task prompt", [{"name": "Read"}])
    assert _task_inference_request([unrelated, task], agent, "task prompt", after=1) is task


def test_cuj_task_inference_selection_uses_exact_tool_prompt():
    prompt = "Read input-file.txt using a tool. Reply with only its contents."
    claude = [
        _inference_request(CLAUDE, 1, f"Name this session: {prompt}", []),
        _inference_request(CLAUDE, 2, prompt, []),
        _inference_request(CLAUDE, 3, prompt, [{"name": "Read"}]),
    ]
    assert _task_inference_request(claude, CLAUDE, prompt) is claude[2]
    child_prompt = "Read input-file.txt using a tool and return its contents."
    parent = _inference_request(
        CODEX, 4, "Delegate this task to one subagent.", [{"name": "spawn_agent"}]
    )
    child = _inference_request(CODEX, 5, child_prompt, [{"name": "read_file"}])
    assert _task_inference_request([parent, child], CODEX, child_prompt, after=3) is child
    with pytest.raises(AssertionError, match="No tool-capable"):
        _task_inference_request([parent, child], CODEX, child_prompt, after=5)


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
@pytest.mark.parametrize("after", [0, 1])
@pytest.mark.parametrize("suffix", ["\n", "\n\n", " ", "\nDifferent task."])
def test_cuj_task_inference_only_allows_claude_child_transport_newline(agent, after, suffix):
    prompt = "Read input-file.txt using a tool and return its contents."
    request = _inference_request(agent, 2, prompt + suffix, [{"name": "Read"}])
    if agent == CLAUDE and after and suffix == "\n":
        assert _task_inference_request([request], agent, prompt, after=after) is request
    else:
        with pytest.raises(AssertionError, match="No tool-capable"):
            _task_inference_request([request], agent, prompt, after=after)


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


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
@pytest.mark.parametrize("failure", [None, "role", "prompt", "model", "schema", "status", "body"])
def test_cuj_inference_evidence_requires_exact_task_model_schema_and_response(agent, failure):
    task = SimpleNamespace(prompt="Read the fixture and return its contents")
    model = f"ug_e2e.models.{'claude_haiku' if agent == CLAUDE else 'gpt_luna'}"
    field = "messages" if agent == CLAUDE else "input"
    content_type = "text" if agent == CLAUDE else "input_text"
    payload = {
        "model": "wrong-model" if failure == "model" else model,
        field: [
            {
                "role": "assistant" if failure == "role" else "user",
                "content": [
                    {
                        "type": content_type,
                        "text": f"Quoted: {task.prompt}" if failure == "prompt" else task.prompt,
                    }
                ],
            }
        ],
    }
    request = RecordedRequest(
        sequence=1,
        method="POST",
        path=INFERENCE_PATHS[agent],
        headers={
            MODEL_SERVICE_PARENT_SCHEMA_HEADER.lower(): (
                "ug_e2e.other_models" if failure == "schema" else "ug_e2e.models"
            )
        },
        body=json.dumps(payload).encode(),
    )
    response = RecordedResponse(
        status_code=403 if failure == "status" else 200,
        headers={},
        body=b"" if failure == "body" else b"model response",
    )
    recorder = SimpleNamespace(
        requests_after=lambda checkpoint: (request,),
        response_for=lambda matched, timeout: response,
    )
    if failure is None:
        assert_inference_evidence(recorder, 0, agent, task, model, parent_schema="ug_e2e.models")
    else:
        with pytest.raises(AssertionError):
            assert_inference_evidence(
                recorder, 0, agent, task, model, parent_schema="ug_e2e.models"
            )


@pytest.mark.parametrize("exclusive", [True, False])
@pytest.mark.parametrize("failure", [None, "model", "tokens", "error", "side_model"])
def test_cuj_claude_headless_model_requires_expected_model_output_tokens(failure, exclusive):
    model = "ug_e2e.models.claude_haiku"
    usage = {
        "wrong-model" if failure == "model" else model: {
            "outputTokens": 0 if failure == "tokens" else 1
        }
    }
    if failure == "side_model":
        usage["ug_e2e.models.claude_sonnet"] = {"outputTokens": 1}
    result = SimpleNamespace(
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": failure == "error",
                "modelUsage": usage,
            }
        )
    )
    if failure is None or (failure == "side_model" and not exclusive):
        assert_claude_headless_model(result, model, exclusive=exclusive)
    else:
        with pytest.raises(AssertionError):
            assert_claude_headless_model(result, model, exclusive=exclusive)
