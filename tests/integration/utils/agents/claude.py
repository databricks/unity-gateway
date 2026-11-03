"""Claude-specific integration evidence and model discovery helpers."""

from ..model_discovery import claude_discovery_model_id as discovery_model_id
from ..model_discovery import claude_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
    "assert_agent_resumed",
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


def assert_agent_resumed(call: dict, records: list[dict], agent_id: str) -> None:
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
            assert isinstance(result, dict) and result.get("resumedAgentId") == agent_id, (
                f"SendMessage to {recipient!r} did not resume {agent_id}: {result}"
            )
            return
    raise AssertionError(f"No native tool result for SendMessage {call['id']}: {call}")


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
