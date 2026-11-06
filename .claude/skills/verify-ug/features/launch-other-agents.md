---
feature: launch-other-agents
title: Launch gemini, opencode, kilo, copilot, pi, antigravity and cursor
platform: cli
paths: [src/ucode/cli.py, src/ucode/agents/gemini.py, src/ucode/agents/opencode.py, src/ucode/agents/kilo.py, src/ucode/agents/copilot.py, src/ucode/agents/pi.py, src/ucode/agents/antigravity.py, src/ucode/agents/cursor.py]
status: 'unverified: opencode and cursor driven with -- --version; gemini, kilo, copilot, pi and antigravity binaries are not installed here. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Launch the other agents

`ug <agent>` configures if needed and execs the agent (`cli.py:4724-4860`: kilo 4728, copilot 4750, pi 4786, antigravity 4799 with hidden alias `agy`, cursor 4838).

## Reach

Same isolated-HOME recipe as `launch-claude`. `command -v` found only `opencode`, `cursor`/`cursor-agent`, `claude`, `codex` and `code`.

## Drive

1. [verified] `ug opencode -- --version`
2. [verified] `ug cursor -- --version`
3. [unverified] `ug gemini|kilo|copilot|pi|antigravity|agy`. Binary absent. A launch runs `install_tool_binary`, which may install the agent, so it was not run.
4. [unverified] `--provider` on gemini, copilot and antigravity. `--model` on gemini, opencode (`-m`), kilo, antigravity.

## Expect

- Step 1 exits 0. Output: "Launching OpenCode with Unity Gateway", "OpenCode token refresh is managed automatically every 30 minutes while the session is running.", "Starting OpenCode", then `1.17.13`. Files: `.ucode/opencode-xdg/opencode/opencode.json`, `.ucode/opencode-xdg/opencode/plugin/ucode-auth.js`, `.ucode/opencode-config.backup.json`. XDG is redirected into `.ucode`, so the user's real OpenCode config is untouched.
- Step 2 exits 0. Output: "Unity Gateway with Cursor", "Cursor runs models on your Cursor account; its Databricks MCP servers authenticate through ug mcp-proxy.", "Starting Cursor Agent", then `2026.11.05-86047df`. Cursor is MCP-only: no model routing, and it writes `.cursor/mcp.json` and `.cursor/cli-config.json` only via `ug mcp add`.
- Token-refreshing agents (gemini, opencode, copilot, pi) spawn a child and wait (`launcher.py` comment); exit code is passed through.

## Gotchas

- `ug configure --agents cursor` only establishes workspace state (`cli.py:~5168`). Use `ug mcp add --agents cursor` for the servers.
- `ug agents add <agent>` self-manages an agent a managed config doesn't enable. It forces a browser login, so it is not drivable headlessly.
