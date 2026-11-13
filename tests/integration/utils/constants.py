"""Shared constants for the integration CUJs."""

CLAUDE_TEST_MODEL = "system.ai.claude-haiku-4-5"
CLAUDE_RELAY_TEST_MODEL = "claude-haiku-4-5-20251001"
CODEX_TEST_MODEL = "system.ai.gpt-6-luna"
COPILOT_GPT_TEST_MODEL = "system.ai.gpt-6-luna"
# Open models Copilot calls on Chat Completions, one per family ug registers.
COPILOT_GLM_TEST_MODEL = "system.ai.glm-5-2"
COPILOT_KIMI_TEST_MODEL = "system.ai.kimi-k3"
COPILOT_DEEPSEEK_TEST_MODEL = "system.ai.deepseek-v4-1-flash-1017"

# A model the ChatGPT plan serves through the relay. The gateway's relayed /codex/v1/models returns
# the API catalog, so Codex's bundled default is rejected for a ChatGPT account; pin this one.
CODEX_RELAYED_MODEL = "gpt-6-luna"

COPILOT_HEADLESS_FLAGS = (
    "--allow-all-tools",
    "--output-format",
    "json",
    "--no-auto-update",
    "--disable-builtin-mcps",
)

MANAGED_CLAUDE_PROVIDER_SERVICE = "main.ucode.dont_delete_ci_company_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
