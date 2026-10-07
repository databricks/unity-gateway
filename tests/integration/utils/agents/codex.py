"""Codex-specific integration evidence and model discovery helpers."""

from ..model_discovery import codex_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
    "completed_task_models",
    "model_in_picker",
]

SESSION_DIRECTORY = ".codex/sessions"
EVIDENCE_KIND = "client-selected model"


def is_child_session(path: str, records: list[dict]) -> bool:
    del path
    return any(
        record.get("type") == "session_meta"
        and isinstance(record.get("payload", {}).get("source"), dict)
        and "subagent" in record["payload"]["source"]
        for record in records
    )


def assistant_answers(records: list[dict]) -> list[str]:
    answers = []
    for record in records:
        if record.get("type") == "event_msg":
            payload = record.get("payload", {})
            if payload.get("type") == "task_complete" and payload.get("last_agent_message"):
                answers.append(payload["last_agent_message"])
    return answers


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """Join Codex's completed answer turn to its client-selected context model."""
    assert isinstance(answer_value, str) and answer_value.strip(), (
        "Expected a nonempty answer value"
    )
    completed: set[str] = set()
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        assert isinstance(payload, dict), "Malformed Codex event payload"
        if payload.get("type") != "task_complete":
            continue
        answer = payload.get("last_agent_message")
        assert isinstance(answer, str), "Missing or malformed Codex completed answer"
        if answer_value in answer:
            completed.add(_evidence_id(payload.get("turn_id"), "turn ID"))
    models: dict[str, set[str]] = {turn_id: set() for turn_id in completed}
    for record in records:
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        assert isinstance(payload, dict), "Malformed Codex turn context"
        turn_id = _evidence_id(payload.get("turn_id"), "turn ID")
        if turn_id in completed:
            models[turn_id].add(_evidence_id(payload.get("model"), "model"))
    assert all(models.values()), "Missing Codex model context for completed answer turn"
    return {model for turn_models in models.values() for model in turn_models}
