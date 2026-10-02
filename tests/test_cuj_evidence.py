"""Offline negative controls for the live CUJ's independently written evidence reader."""

import copy
import json
from types import SimpleNamespace

import pytest

from tests.e2e_cuj.helpers.evidence import (
    SessionEvidence,
    canonical_model,
    completed_turn,
)
from tests.integration.utils.evidence import FileTask, read_jsonl


def records(agent, task, model):
    if agent == "claude":
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
    return [
        {"type": "session_meta", "payload": {"id": "session", "source": "cli"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
        {"type": "turn_context", "payload": {"turn_id": "turn", "model": model}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": task.prompt}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn",
                "last_agent_message": task.value,
            },
        },
    ]


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def log(agent, task, model):
    if agent == "claude":
        return f"12:00:00 [ROUTE] first prompt -> '{model}'\n12:00:01 [DONE] first prompt confirmed submitted\n"
    body = {
        "task": {"prompt": task.prompt},
        "route_selector": {"router_name": "task_v3"},
        "route_options": [{"harness": "codex", "model": model}],
    }
    return (
        "[ROUTE] request POST https://example.test/ai-gateway/routing/v1/routes:select: "
        + json.dumps(body)
        + f"\n[ROUTE] selected '{model}'; rationale='test'\n"
    )


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("routed", [True, False])
def test_cuj_evidence_completed_native_turn_and_model(tmp_path, agent, routed):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    model = "system.ai.claude-opus-4-8" if agent == "claude" else "system.ai.gpt-6-luna"
    boundary = SessionEvidence(tmp_path, agent)
    write_rows(boundary.directory / "new.jsonl", records(agent, task, model))
    route_path = next(iter(boundary.boundaries))
    route_path.parent.mkdir(exist_ok=True)
    route_path.write_text(log(agent, task, model) if routed else "")
    assert task.value not in task.prompt
    result = boundary.assert_applied(task, {model}, routed=routed, expected=model)
    assert result["selected_model"] == model


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "failure",
    [
        "banner",
        "stale",
        "wrong_model",
        "unsupported",
        "no_prompt",
        "incomplete",
        "child",
        "duplicate",
    ],
)
def test_cuj_evidence_rejects_false_positives(tmp_path, agent, failure):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    model = "system.ai.claude-opus-4-8" if agent == "claude" else "system.ai.gpt-6-sol"
    baseline = SessionEvidence(tmp_path, agent)
    path = next(iter(baseline.boundaries))
    path.parent.mkdir(exist_ok=True)
    if failure == "stale":
        path.write_text(log(agent, task, model))
    boundary = SessionEvidence(tmp_path, agent)
    rows = records(agent, task, model if failure != "wrong_model" else "system.ai.wrong")
    if failure == "no_prompt":
        rows = [
            row
            for row in rows
            if row.get("type") != "user" and row.get("payload", {}).get("type") != "user_message"
        ]
    if failure == "incomplete":
        rows.pop()
    if failure == "child":
        if agent == "claude":
            rows[0]["isSidechain"] = True
        else:
            rows[0]["payload"]["source"] = {"subagent": "parent"}
    write_rows(boundary.directory / "new.jsonl", rows)
    if failure != "stale":
        text = (
            "Using Unity Gateway Smart Router. Selected Model: " + model
            if failure == "banner"
            else log(agent, task, model)
        )
        path.write_text(text * (2 if failure == "duplicate" else 1))
    with pytest.raises(AssertionError):
        boundary.assert_applied(
            task, {"system.ai.other"} if failure == "unsupported" else {model}, routed=True
        )


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_cuj_evidence_disabled_rejects_new_routing(tmp_path, agent):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    boundary = SessionEvidence(tmp_path, agent)
    model = "system.ai.gpt-6-sol"
    write_rows(boundary.directory / "new.jsonl", records(agent, task, model))
    path = next(iter(boundary.boundaries))
    path.parent.mkdir(exist_ok=True)
    path.write_text(log(agent, task, model))
    with pytest.raises(AssertionError, match="new routing activity"):
        boundary.assert_applied(task, {model}, routed=False, expected=model)


def test_cuj_evidence_codex_rejects_wrong_turn_and_router_prompt(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    rows = records("codex", task, "gpt-6-sol")
    wrong_turn = copy.deepcopy(rows)
    wrong_turn[-1]["payload"]["turn_id"] = "different-turn"
    assert completed_turn("codex", wrong_turn, task) is None
    boundary = SessionEvidence(tmp_path, "codex")
    write_rows(boundary.directory / "new.jsonl", rows)
    path = next(iter(boundary.boundaries))
    path.parent.mkdir(exist_ok=True)
    path.write_text(log("codex", SimpleNamespace(prompt="different prompt"), "gpt-6-sol"))
    with pytest.raises(AssertionError):
        boundary.assert_applied(task, {"system.ai.gpt-6-sol"}, routed=True)


def test_cuj_evidence_ignores_existing_session_and_detects_truncated_log(tmp_path):
    task = FileTask(SimpleNamespace(cwd=tmp_path))
    first = SessionEvidence(tmp_path, "claude")
    write_rows(first.directory / "old.jsonl", records("claude", task, "system.ai.claude-opus-4-8"))
    path = next(iter(first.boundaries))
    path.parent.mkdir(exist_ok=True)
    path.write_text("old log")
    boundary = SessionEvidence(tmp_path, "claude")
    assert boundary.completed(task) is None
    path.write_text("replacement")
    with pytest.raises(AssertionError, match="rotated/truncated"):
        boundary.new_logs()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("anthropic-aigw-1234abcd-system.ai.claude-opus-4-8[1m]", "system.ai.claude-opus-4-8"),
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
