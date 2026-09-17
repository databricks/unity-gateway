"""Shared constants for the integration CUJs."""

CODEX_TEST_MODEL = "system.ai.gpt-5-4-nano"

MANAGED_CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
MANAGED_CODEX_MODELS = ["system.ai.gpt-5-6-sol"]

# Dedicated ca-central-1 services used only by the managed-discovery fixture variants.
MANAGED_CLAUDE_PROVIDER_SERVICE = "main.default.ci_e2e_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
