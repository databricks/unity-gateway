"""Shared constants for the integration CUJs."""

import os

CLAUDE_TEST_MODEL = "system.ai.claude-haiku-4-5"
# nano sometimes nests powershell inside Codex's pwsh, breaking `$` expansion in its file reads.
CODEX_TEST_MODEL = "system.ai.gpt-5-6-luna" if os.name == "nt" else "system.ai.gpt-5-4-nano"

# An older Claude than the family default: only the full catalog registers it for Copilot.
COPILOT_OLDER_CLAUDE_MODEL = "system.ai.claude-sonnet-4-5"
COPILOT_HEADLESS_FLAGS = (
    "--allow-all-tools",
    "--output-format",
    "json",
    "--no-auto-update",
    "--disable-builtin-mcps",
)

MANAGED_CLAUDE_PROVIDER_SERVICE = "main.default.ci_e2e_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
