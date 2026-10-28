"""Shared constants for the integration CUJs."""

import os

CLAUDE_TEST_MODEL = "system.ai.claude-haiku-4-5"
# nano sometimes nests powershell inside Codex's pwsh, breaking `$` expansion in its file reads.
CODEX_TEST_MODEL = "system.ai.gpt-5-6-luna" if os.name == "nt" else "system.ai.gpt-5-4-nano"

COPILOT_HEADLESS_FLAGS = (
    "--allow-all-tools",
    "--output-format",
    "json",
    "--no-auto-update",
    "--disable-builtin-mcps",
)

# Least privilege for a headless file task: Grok may read files and is refused everything else.
GROK_HEADLESS_FLAGS = (
    "--no-auto-update",
    "--permission-mode",
    "dontAsk",
    "--allow",
    "Read",
    "--output-format",
    "streaming-json",
)

MANAGED_CLAUDE_PROVIDER_SERVICE = "main.default.ci_e2e_anthropic_mps"
MANAGED_CODEX_PROVIDER_SERVICE = "main.default.ci_e2e_openai_mps"
