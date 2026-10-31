"""Shared constants for the integration CUJs."""

import os

CLAUDE_TEST_MODEL = "system.ai.claude-haiku-4-5"
# nano sometimes nests powershell inside Codex's pwsh, breaking `$` expansion in its file reads.
CODEX_TEST_MODEL = "system.ai.gpt-5-6-luna" if os.name == "nt" else "system.ai.gpt-5-4-nano"

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

MANAGED_CLAUDE_PROVIDER_SERVICE = "main.default.ci_e2e_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
