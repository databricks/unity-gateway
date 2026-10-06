"""Agent, metastore fixture, and transcript values shared by CUJs."""

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


CODING_AGENT_BY_CLI_NAME = {
    CLAUDE: CodingAgent.CLAUDE_CODE,
    CODEX: CodingAgent.CODEX,
}

MODEL_PROVIDER_SERVICE_FIXTURES = {
    CLAUDE: ("ug_e2e.providers.anthropic", "claude-haiku-4-5-20251001"),
    CODEX: ("ug_e2e.providers.openai", "gpt-5-nano"),
}

SANDBOX_MCP_SERVICE_NAME = "system.ai.sandbox"
WEB_SEARCH_MCP_SERVICE_NAME = "system.ai.web_search"
