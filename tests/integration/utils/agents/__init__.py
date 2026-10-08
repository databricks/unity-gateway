"""Shared validation for agent-specific integration evidence."""

import re


def _evidence_id(value: object, field: str) -> str:
    assert isinstance(value, str) and re.fullmatch(r"\S+", value), (
        f"Missing or malformed completed-task {field}"
    )
    return value
