"""CLI agent names and public API wire values shared by CUJs."""

from enum import StrEnum

CLAUDE = "claude"
CODEX = "codex"
INFERENCE_PATHS = {
    CLAUDE: "/ai-gateway/anthropic/v1/messages",
    CODEX: "/ai-gateway/codex/v1/responses",
}


class CodingAgent(StrEnum):
    CLAUDE_CODE = "CODING_AGENT_CLAUDE_CODE"
    CODEX = "CODING_AGENT_CODEX"
