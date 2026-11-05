"""Claude-specific integration evidence and model discovery helpers."""

from typing import Literal

from ..model_discovery import claude_discovery_model_id as discovery_model_id
from ..model_discovery import claude_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
    "assert_follow_ups_resumed",
    "classify_send_message",
    "completed_task_models",
    "discovery_model_id",
    "model_in_picker",
]

SESSION_DIRECTORY = ".claude/projects"
EVIDENCE_KIND = "response-reported model"


def is_child_session(path: str, records: list[dict]) -> bool:
    del records
    return "/subagents/" in path


def assistant_answers(records: list[dict]) -> list[str]:
    answers = []
    for record in records:
        message = record.get("message", {})
        if record.get("type") == "assistant" and message.get("role") == "assistant":
            for part in message.get("content", []):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    answers.append(part["text"])
                elif _is_subagent_handback(record, part):
                    answers.append(part["input"]["message"])
    return answers


def _is_subagent_handback(record: dict, part: dict) -> bool:
    return (
        record.get("isSidechain") is True
        and part.get("type") == "tool_use"
        and part.get("name") == "SubagentHandback"
        and isinstance(part.get("id"), str)
        and isinstance(part.get("input", {}).get("message"), str)
    )


SendOutcome = Literal["resumed", "queued", "rejected"]


def _is_queued_to(result: object, agent_id: str) -> bool:
    """True for a successful SendMessage result queued to the still-running `agent_id`."""
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and "resumedAgentId" not in result
        and "queued" in str(result.get("message", "")).lower()
        and isinstance(result.get("pin"), dict)
        and result["pin"].get("id") == agent_id
    )


def _is_rejected_for(call: dict, result: object, agent_id: str) -> bool:
    """True for a failed (undelivered) SendMessage addressed to `agent_id`.

    Claude's rejection (for example an unsupported `notify_when_idle`) carries only
    `success: False` and a message, so the child is identified by the call's `to` or,
    when present, the result's `pin.id`.
    """
    if not isinstance(result, dict) or result.get("success") is not False:
        return False
    if "resumedAgentId" in result:
        return False
    pin = result.get("pin")
    return call["input"]["to"] == agent_id or (isinstance(pin, dict) and pin.get("id") == agent_id)


def classify_send_message(call: dict, records: list[dict], agent_id: str) -> SendOutcome:
    """Classify one parent SendMessage for the original child `agent_id`.

    Returns "resumed" (the native result resolved to the child), "queued" (accepted for
    the still-running child) or "rejected" (addressed to the child but not delivered).
    Raises AssertionError for a missing recipient or result, a result for another agent,
    or any other shape.
    """
    recipient = call.get("input", {}).get("to")
    assert isinstance(recipient, str) and recipient.strip(), (
        f"SendMessage requires a nonempty 'to' recipient: {call}"
    )
    for record in records:
        content = record.get("message", {}).get("content")
        if record.get("type") != "user" or not isinstance(content, list):
            continue
        if any(part.get("tool_use_id") == call["id"] for part in content):
            result = record.get("toolUseResult")
            # The native result resolves both names and IDs to the resumed child's ID.
            if isinstance(result, dict) and result.get("resumedAgentId") == agent_id:
                return "resumed"
            if _is_queued_to(result, agent_id):
                return "queued"
            if _is_rejected_for(call, result, agent_id):
                return "rejected"
            raise AssertionError(
                f"SendMessage to {recipient!r} neither resumed nor was queued or rejected "
                f"for {agent_id}: {result}"
            )
    raise AssertionError(f"No native tool result for SendMessage {call['id']}: {call}")


def assert_follow_ups_resumed(calls: list[dict], records: list[dict], agent_id: str) -> None:
    """Require every SendMessage to target the child and at least one to resume it.

    A redundant follow-up the model sends while the child runs may be queued or rejected;
    that is tolerated only alongside a real resume.
    """
    assert calls, "Expected at least one SendMessage"
    outcomes = [classify_send_message(call, records, agent_id) for call in calls]
    assert "resumed" in outcomes, f"No SendMessage resumed {agent_id}: {outcomes}"


def completed_task_models(records: list[dict], answer_value: str) -> set[str]:
    """Parse Claude's response-reported model on the assistant's matching answer."""
    assert isinstance(answer_value, str) and answer_value.strip(), (
        "Expected a nonempty answer value"
    )
    found: set[str] = set()
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        assert isinstance(message, dict), "Malformed Claude assistant message"
        assert message.get("role") == "assistant", "Missing Claude assistant role"
        content = message.get("content")
        assert isinstance(content, list), "Malformed Claude assistant content"
        for part in content:
            assert isinstance(part, dict), "Malformed Claude assistant content block"
            if part.get("type") != "text":
                continue
            text = part.get("text")
            assert isinstance(text, str), "Malformed Claude assistant text"
            if answer_value in text:
                model = _evidence_id(message.get("model"), "model")
                found.add(_evidence_id(model.removesuffix("[1m]"), "model"))
    return found
