---
feature: agents-manage
title: ug agents (list, add, remove)
platform: cli
paths: [src/ucode/cli.py, src/ucode/state.py]
status: 'unverified: list and remove driven (seeded and staging); add forces a browser login and was not completed'
---

# ug agents

Manage the self-managed agent list for the saved workspace. Group `agents_app` `cli.py:2104`; `add` `cli.py:2126`, `remove` `cli.py:2168`, `list` `cli.py:2196`.

## Reach

From a shell, run `ug agents <subcommand>` with the isolated environment in `../SKILL.md` (Environment).

## Drive

Env: `HOME=$H UG_SKIP_OS_SETTINGS=1`, stdin `</dev/null`. Seed `$H/.ucode/state.json` as in the status entry and a working `databricks` on PATH (see SKILL.md Environment).

1. [verified] Unconfigured: `ug agents list`, `ug agents remove opencode`.
2. [verified] `ug agents remove bogus`.
3. [verified] `ug agents` (bare).
4. [verified] Seeded workspace: `ug agents list`, `ug agents remove claude`.
5. [verified] Add `"self_managed_agents": ["opencode"]` to the workspace bucket (`state.py:378`), then `ug agents list` and `ug agents remove opencode` twice.
6. [unverified: forces browser login] `ug agents add <agent>` runs full agent setup (login, model discovery, config writes) and records the agent as self-managed (`cli.py:2126-2165`). On staging with a cached login, `ug agents add opencode` still printed an OIDC URL and waited for a browser (exit 124 under `timeout`). With no workspace it exits 1: "No workspace configured. Run `ug configure` first."
7. [verified, staging workspace] With the staging managed config cached, `ug agents list` shows Claude Code and Codex as `admin-managed`.

## Expect

- 1: exit 1, `ERROR No workspace configured. Run `ug configure` first.`; no files.
- 2: exit 1, `Unknown agent 'bogus'. Valid agents: antigravity, claude, codex, copilot, gemini, opencode, pi.`
- 3: exit 2, Rich help listing add, remove, list.
- 4: exit 0. list: heading `Agents`, `Workspace: <url>`, "No managed config is published for this workspace.", "No agents set up for this workspace yet."; remove: "Claude Code is not in your self-managed list."
- 5: list shows table `AGENT STATUS` with `OpenCode self-managed`; first remove exits 0 with "Removed OpenCode from your self-managed list."; second prints "not in your self-managed list."

## Gotchas

- A seeded state is rewritten and expanded on first load (adds `base_urls`, `agents`, `managed_configs`); do not diff it against the seed.
- list/remove call `_fetch_managed_config` (the Databricks CLI): they write `.databricks/token-cache.json` and `.cache/databricks/<ver>/host-metadata-negative/*.json` under HOME and tried to reach the fake host. No managed config came back, exit 0.
- Without a working `databricks` on PATH, fresh-HOME behavior with a seeded workspace was not driven (the no-workspace error fires first).
