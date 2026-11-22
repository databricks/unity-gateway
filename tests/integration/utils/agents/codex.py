"""Codex-specific integration evidence and model discovery helpers."""

import hashlib
import json

from ..model_discovery import codex_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assert_agent_interaction_completed",
    "assistant_answers",
    "completed_task_models",
    "completed_command_outputs",
    "completed_smart_router_results",
    "routed_assignment_digests",
    "model_in_picker",
]

SESSION_DIRECTORY = ".codex/sessions"
EVIDENCE_KIND = "client-selected model"


def routed_assignment_digests(sessions: dict[str, list[dict]], marker: str) -> set[str]:
    """Match plaintext assignments or encrypted delivery with its original task-name fallback."""
    records = [row for rows in sessions.values() for row in rows]
    digests = set()
    encrypted_deliveries = set()
    for row in records:
        item = row.get("payload", {})
        if row.get("type") != "response_item" or item.get("type") != "agent_message":
            continue
        for part in item.get("content", []):
            text = part.get("text", "")
            if part.get("type") != "input_text" or not text.startswith("Message Type: NEW_TASK\n"):
                continue
            header, separator, assignment = text.partition("\nPayload:\n")
            if not separator:
                continue
            if marker in assignment:
                digests.add(hashlib.sha256(assignment.strip().encode("utf-8")).hexdigest())
            elif not assignment.strip() and marker in header:
                encrypted_deliveries.update(
                    content["encrypted_content"]
                    for content in item.get("content", [])
                    if content.get("type") == "encrypted_content"
                    and isinstance(content.get("encrypted_content"), str)
                    and content["encrypted_content"]
                )
    for row in records:
        item = row.get("payload", {})
        if (
            row.get("type") != "response_item"
            or item.get("type") != "function_call"
            or not item.get("name", "").endswith("spawn_agent")
        ):
            continue
        arguments = json.loads(item["arguments"])
        message, name = arguments.get("message"), arguments.get("task_name")
        if (
            isinstance(message, str)
            and message in encrypted_deliveries
            and isinstance(name, str)
            and marker in name
        ):
            digests.add(hashlib.sha256(name.strip().encode("utf-8")).hexdigest())
    return digests


def assert_agent_interaction_completed(records: list[dict], target: str, thread_id: str) -> None:
    """Require the native delivery receipt, shared by direct tools and code mode."""
    assert any(
        row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "item_completed"
        and (item := row["payload"].get("item", {})).get("type") == "SubAgentActivity"
        and item.get("kind") == "interacted"
        and item.get("agent_path") == target
        and item.get("agent_thread_id") == thread_id
        for row in records
    ), f"No successful native interaction with {target} ({thread_id})"


def _command_text(command: object) -> str:
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(part, str) for part in command):
        return " ".join(command)
    return ""


def completed_command_outputs(
    records: list[dict], *, command_contains: str | None = None
) -> list[str]:
    return [
        item["aggregated_output"]
        for row in records
        if row.get("type") == "event_msg"
        and row.get("payload", {}).get("type") == "item_completed"
        and (item := row["payload"].get("item", {})).get("type") == "CommandExecution"
        and item.get("status") == "completed"
        and item.get("exit_code") == 0
        and isinstance(item.get("aggregated_output"), str)
        and (command_contains is None or command_contains in _command_text(item.get("command")))
    ]


def completed_smart_router_results(records: list[dict], action: str) -> list[dict]:
    results = []
    for row in records:
        if row.get("type") != "event_msg" or row.get("payload", {}).get("type") != "item_completed":
            continue
        item = row["payload"].get("item", {})
        if item.get("type") != "DynamicToolCall" or item.get("tool") != "ug_smart_router":
            continue
        if item.get("arguments", {}).get("action") != action:
            continue
        if (
            action == "off"
            and results
            and item.get("status") == "failed"
            and item.get("success") is False
            and item.get("content_items")
            == [
                {
                    "type": "inputText",
                    "text": "Use $smart-router off to turn Smart Router off. Routing is unchanged.",
                }
            ]
        ):
            continue
        assert item.get("status") == "completed" and item.get("success") is True, item
        contents = item.get("content_items")
        assert isinstance(contents, list) and len(contents) == 1, item
        assert contents[0].get("type") == "inputText", item
        result = json.loads(contents[0]["text"])
        assert isinstance(result, dict), item
        results.append(result)
    return results


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
