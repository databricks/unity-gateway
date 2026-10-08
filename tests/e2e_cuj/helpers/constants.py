"""Agent, model, header, metastore fixture, and transcript values shared by CUJs."""

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
CLAUDE_OPUS_MODEL = "system.ai.claude-opus-4-8"
CLAUDE_SONNET_MODEL = "system.ai.claude-sonnet-4-6"
CLAUDE_HAIKU_MODEL = "system.ai.claude-haiku-4-5"
# Claude reports Bedrock-routed Haiku under this native id.
NATIVE_MODEL_ALIASES = {
    "system.ai.anthropic.claude-haiku-4-5-20251001-v1:0": CLAUDE_HAIKU_MODEL,
}
CODEX_SOL_MODEL = "system.ai.gpt-5-6-sol"
CODEX_LUNA_MODEL = "system.ai.gpt-5-6-luna"
# TuiRequestRecorder lowercases recorded header names.
RUN_HEADER = "x-ug-e2e-run"
AGENT_HEADER = "x-ug-e2e-agent"
PROVIDER_HEADER = "databricks-model-provider-service"


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

BEDROCK_PROVIDER_SERVICE_FIXTURE = (
    "ug_e2e.providers.bedrock",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
UC_MODEL_LOCATION_FIXTURE = ("ug_e2e.models", "ug_e2e.models.codex_primary")
FIXTURE_READER_MCP_SERVICE_NAME = "ug_e2e.tools.fixture_reader"
# UC skill names reject underscores, so the plan's `fixture_summary` is hyphenated.
FIXTURE_SUMMARY_SKILL_NAME = "ug_e2e.skills.fixture-summary"
FIXTURE_NOTES_SKILL_NAME = "ug_e2e.skills.fixture-notes"
FIXTURE_CATALOG_LOOKUP_SKILL_NAME = "ug_e2e.skills.fixture-catalog-lookup"
FIXTURE_DECOY_SKILL_NAME = "ug_e2e.other_skills.fixture-decoy"
SKILLS_LOCATION = "ug_e2e.skills"
