"""Claude-specific integration evidence and model discovery helpers."""

from ..model_discovery import claude_discovery_model_id as discovery_model_id
from ..model_discovery import claude_model_in_picker as model_in_picker
from . import _evidence_id

__all__ = [
    "EVIDENCE_KIND",
    "assistant_answers",
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
    handbacks = {}
    for record in records:
        message = record.get("message", {})
        if record.get("type") == "assistant" and message.get("role") == "assistant":
            for part in message.get("content", []):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    answers.append(part["text"])
                elif _is_subagent_handback(record, part):
                    # Claude 2.1.290 can deliver a child's final answer through this
                    # tool. The call alone is not evidence that delivery succeeded.
                    handbacks[part["id"]] = part["input"]["message"]
        elif _is_successful_child_turn_completion(record):
            for part in message.get("content", []):
                if part.get("type") == "tool_result" and not part.get("is_error"):
                    answer = handbacks.pop(part.get("tool_use_id"), None)
                    if answer is not None:
                        answers.append(answer)
    return answers


def _is_subagent_handback(record: dict, part: dict) -> bool:
    return (
        record.get("isSidechain") is True
        and part.get("type") == "tool_use"
        and part.get("name") == "SubagentHandback"
        and isinstance(part.get("id"), str)
        and isinstance(part.get("input", {}).get("message"), str)
    )


def _is_successful_child_turn_completion(record: dict) -> bool:
    return (
        record.get("type") == "user"
        and record.get("message", {}).get("role") == "user"
        and record.get("isSidechain") is True
        and record.get("toolEndsTurn") is True
        and record.get("toolUseResult", {}).get("success") is True
    )


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
