---
feature: launch-claude
title: Launch Claude Code through the gateway (ug claude)
platform: cli
paths: [src/ucode/cli.py, src/ucode/launcher.py, src/ucode/agents/claude.py, src/ucode/smart_routing/**]
status: 'unverified: steps 1-5 driven with -- --version; prompt argument and --provider not run. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Launch Claude Code (ug claude)

Auto-configure if needed, then exec `claude` with gateway settings (`cli.py:4580`, `_launch_tool` at `cli.py:3768`).

## Reach

Needs a workspace in `$H/.ucode/state.json`. A first `ug mcp add --workspace <url> --location system.ai --names system.ai.dbsql --agents cursor` creates it without forced login.
Drive with the isolated-HOME recipe from `configure-agent`.
Set `UCODE_MANAGED_CONFIG_STUB=$H/stub-null.json` (file content `null`) if the workspace publishes a managed config with an unusable provider.
Unset every inherited smart-router variable (`ENABLE_SMART_ROUTING_V2`, `ENABLE_SMART_ROUTER_ORCHESTRATOR`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_GATEWAY`, `UG_HEADERS`, ...) when you run from inside a Claude session.

## Drive

Use the native `--version` so no model call happens.

1. `ug claude -- --version`
2. `ug claude --model system.ai.claude-sonnet-5-5 -- --version`
3. `ug claude --refresh --skip-preflight -- --version`
4. `ug claude --enable-smart-routing -- --version` and `--disable-smart-routing`
5. `SMART_ROUTER_CONFIG_VERSION=subagent_only_v1 ug claude -- --version`, and `=bogus_v9`
6. [unverified] Prompt argument (`ug claude "hi"`) and `--provider`, `--model-location`, `--client-id`: not run (would call a model, or need a provider service).

## Expect

- Each of steps 1-5 exits 0. The output ends with `Starting Claude Code` and then `2.1.314 (Claude Code)`.
- First run in a HOME prints "Configuration Complete ... Claude Code: configured (Provider: Databricks)", then "No managed coding agent config found; using your own settings" (stub case).
- Files written: `.claude/ucode-settings.json` (keys `env apiKeyHelper permissions modelPicker`), `.claude.json` plus `.claude/backups/`, `.ucode/state.json`, `.ucode/managed-config.json`, `.ucode/skills.json`. With smart routing also `.claude/skills/smart-router/` and `.claude/skills/smart-router-orchestrator/`.
- Smart routing prints "Claude Code may require one-time hook review. Open /hooks ..." (with --enable-smart-routing or a known version; not with --disable-smart-routing or an unknown one). The bogus version prints nothing and launches normally.
- `--model nonexistent-x` with `-- --version` still exits 0. The model is not validated before exec.

## Gotchas

- `ug --dry-run claude` is NOT a dry run. `set_dry_run` runs after the subcommand early-return (`cli.py:4397-4400`). Observed: it launched Claude Code (`2.1.314`, exit 0). Only bare `ug --dry-run` is dry.
- Without a state, `ug claude` falls into an interactive workspace picker: with stdin not a terminal it prints "Input is not a terminal" and exits 1.
- A workspace whose managed config pins a provider `<managed provider>`, if that provider returns no Anthropic models, Plain `ug claude` exits 1 with "Could not discover Claude models for Model Provider Service". The stub-null workaround is above.
- Inherited smart-routing env from an outer session makes the launch fail ("smart routing cannot be enabled with --provider") or alter behavior.
- `exec_or_spawn` replaces the process on POSIX (`launcher.py:12`), so the exit code is claude's.
