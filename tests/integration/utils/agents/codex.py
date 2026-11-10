"""Codex-specific integration evidence and model discovery helpers."""

from ..model_discovery import codex_model_in_picker as model_in_picker
from . import CompletedTurn, _evidence_id, message_text

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


def completed_prompt_turn(records: list[dict], prompt: str) -> CompletedTurn | None:
    meta = [row["payload"] for row in records if row.get("type") == "session_meta"]
    if len(meta) != 1 or isinstance(meta[0].get("source"), dict):
        return None
    contexts, active_turn, target_turn = {}, None, None
    started_turn, aborted_turn, answer, prompt_sources = None, None, None, set()
    for row in records:
        payload = row.get("payload", {})
        metadata = row.get("internal_chat_message_metadata_passthrough") or {}
        if "multi_agent.subagent_notification" in metadata.get(
            "content_item_kinds", ()
        ) or "<subagent_notification>" in str(
            payload.get("content") or payload.get("message")
        ).replace("-", "_"):
            continue
        row_turn = payload.get("turn_id")
        if row.get("type") == "turn_context":
            contexts.setdefault(row_turn, []).append(payload.get("model"))
            if target_turn and row_turn != target_turn:
                return None
            active_turn = row_turn
            continue
        source = None
        if row.get("type") == "event_msg" and payload.get("type") == "user_message":
            source, submitted_prompt = "event", payload.get("message")
        elif (
            row.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            source, submitted_prompt = "response", message_text(payload.get("content"))
        if source:
            if submitted_prompt == prompt:
                assert source not in prompt_sources, "Prompt was submitted more than once"
                prompt_sources.add(source)
                if target_turn and row_turn and row_turn != target_turn:
                    return None
                target_turn = target_turn or row_turn or active_turn
            elif prompt_sources:
                return None
            continue
        if row.get("type") != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type == "task_started":
            turn_id = payload.get("turn_id")
            if not turn_id or turn_id == aborted_turn:
                return None
            if target_turn and turn_id != target_turn:
                return None
            active_turn = started_turn = turn_id
            if prompt_sources and target_turn is None:
                target_turn = turn_id
        elif event_type == "task_complete":
            turn_id = payload.get("turn_id")
            if target_turn:
                if turn_id != target_turn:
                    return None
                answer = payload.get("last_agent_message") or ""
                break
            if turn_id == active_turn:
                active_turn = started_turn = None
        elif event_type == "turn_aborted":
            aborted_turn = payload.get("turn_id")
            if aborted_turn == target_turn:
                return None
            if aborted_turn == active_turn:
                active_turn = started_turn = None
    if not prompt_sources or not target_turn or started_turn != target_turn or answer is None:
        return None
    models = contexts.get(target_turn)
    if not models:
        return None
    assert all(models), "Missing native turn model metadata"
    return CompletedTurn(meta[0]["id"], target_turn, models, answer)
