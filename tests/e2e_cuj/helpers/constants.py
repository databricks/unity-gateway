"""CLI agent names and public API wire values shared by CUJs."""

from enum import StrEnum
from pathlib import Path

CLAUDE = "claude"
CODEX = "codex"
MANAGED_PATHS = (
    Path("/etc/claude-code/managed-settings.json"),
    Path("/etc/codex/managed_config.toml"),
    Path("/etc/codex/requirements.toml"),
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
)
INFERENCE_PATHS = {
    CLAUDE: "/ai-gateway/anthropic/v1/messages",
    CODEX: "/ai-gateway/codex/v1/responses",
}


class CodingAgent(StrEnum):
    CLAUDE_CODE = "CODING_AGENT_CLAUDE_CODE"
    CODEX = "CODING_AGENT_CODEX"
