"""Shared constants for the integration CUJs."""

CODEX_TEST_MODEL = "system.ai.gpt-5-4-nano"

MANAGED_CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
MANAGED_CODEX_MODELS = ["system.ai.gpt-5-6-sol"]

# These differ from the managed workspace's published list, so the model-discovery cases prove
# their injected CodingAgentConfig—not ambient workspace state—drove the agent catalog.
MANAGED_FIXTURE_CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-5",
]
MANAGED_FIXTURE_CODEX_MODELS = ["system.ai.gpt-5-6-terra"]
