"""Public API wire values shared by independent CUJs."""

from enum import StrEnum


class CodingAgent(StrEnum):
    CLAUDE_CODE = "CODING_AGENT_CLAUDE_CODE"
    CODEX = "CODING_AGENT_CODEX"
