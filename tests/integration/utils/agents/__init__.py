"""Shared validation for agent-specific integration evidence."""

import re
from dataclasses import dataclass


def _evidence_id(value: object, field: str) -> str:
    assert isinstance(value, str) and re.fullmatch(r"\S+", value), (
        f"Missing or malformed completed-task {field}"
    )
    return value


@dataclass
class CompletedTurn:
    session_id: str
    turn_id: str
    models: list[str]
    answer: str


def message_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "") for part in content or [] if part.get("type") in {"text", "input_text"}
    )
