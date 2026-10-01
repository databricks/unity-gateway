"""Shared constants for the integration CUJs."""

CLAUDE_TEST_MODEL = "system.ai.claude-haiku-4-5"
CODEX_TEST_MODEL = "system.ai.gpt-5-4-nano"

CLAUDE_SMART_ROUTING_MODELS = [
    "system.ai.claude-sonnet-5",
    "system.ai.claude-haiku-4-5",
    "system.ai.claude-opus-4-8",
]
CODEX_SMART_ROUTING_MODELS = [
    "system.ai.gpt-5-6-sol",
    "system.ai.gpt-5-6-terra",
    "system.ai.gpt-5-6-luna",
]

MANAGED_CLAUDE_PROVIDER_SERVICE = "main.default.ci_e2e_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
