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
    for record in records:
        if record.get("type") == "assistant":
            message = record.get("message", {})
            if message.get("role") == "assistant":
                answers.extend(
                    part["text"]
                    for part in message.get("content", [])
                    if part.get("type") == "text" and isinstance(part.get("text"), str)
                )
    return answers


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
