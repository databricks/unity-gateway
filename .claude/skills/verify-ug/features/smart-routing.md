---
feature: smart-routing
title: Smart routing and the Smart Router Orchestrator
platform: cli
paths: [src/ucode/smart_routing/**, src/ucode/constants.py, src/ucode/cli.py, src/ucode/agents/claude.py, src/ucode/agents/codex.py, tests/test_smart_routing_config.py]
status: 'unverified: launch flags and version selectors accepted live, but the resolved env and routing behavior were not observed. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Smart routing

Route a Claude or Codex session's subagents (and optionally the first prompt) to other gateway models, with an orchestrator workflow.

## Reach

Controls: `ug claude --enable-smart-routing|--disable-smart-routing` (`cli.py:4635`, Claude only); env `SMART_ROUTER_CONFIG_VERSION`, `ENABLE_SMART_ROUTING_V2`, `ENABLE_SMART_ROUTING_SUBAGENT_ONLY`, `ENABLE_SMART_ROUTER_ORCHESTRATOR` (`constants.py:14-22`, `SMART_ROUTING_ENV_KEYS`). Presets live in `smart_routing/config.py` `_VERSIONS` (`:70`): `first_prompt_and_subagent_no_orch_v0`, `subagent_only_v0`, `subagent_only_v1`, `subagent_orch_v0`, `subagent_orch_v1`, `subagent_orch_v0_claude_only` (default for `--enable-smart-routing`, `config.py:37`).
Use the isolated-HOME launch recipe from `launch-claude`.

## Drive

1. [verified, component only] `from the repo root: uv run pytest tests/test_smart_routing_config.py -q`. Not a live check.
2. [verified] `ug claude --enable-smart-routing -- --version`
3. [verified] `ug claude --disable-smart-routing -- --version`
4. [verified] `SMART_ROUTER_CONFIG_VERSION=subagent_only_v1 ug claude -- --version`
5. [verified] `SMART_ROUTER_CONFIG_VERSION=bogus_v9 ug claude -- --version`
6. [unverified] Env and settings the child actually receives, `/hooks` trust, real subagent routing, Codex path, orchestrator toggles inside a session.

## Expect

- Step 1: `235 passed` in about 0.4 s.
- Steps 2 and 4 exit 0 and print "Claude Code may require one-time hook review. Open /hooks and trust the ug routing hooks if prompted." Step 3 and step 5 print no such note. All print the `--version` line.
- Step 2 writes `.claude/skills/smart-router/` and `.claude/skills/smart-router-orchestrator/` in the temp HOME. The hook handlers are launch-scoped (no `hooks` key lands in `ucode-settings.json`).
- Unknown selector is a no-op (AGENTS.md); step 5 behaves like a plain launch.

## Gotchas

- A smart-routed outer session exports these variables into child shells; unset them (and `UG_HEADERS`, `ANTHROPIC_BASE_URL`) before driving, or launches inherit them. Observed: an inherited env plus a managed-config provider gave "Claude Code smart routing cannot be enabled with --provider" (exit 1).
- A valid nonempty selector overrides conflicting legacy flags; an unset one keeps legacy behavior.
- Adding a parameter requires an entry in every `_VERSIONS` mapping (`_validate_versions` at import); see AGENTS.md "Smart-routing configuration".
