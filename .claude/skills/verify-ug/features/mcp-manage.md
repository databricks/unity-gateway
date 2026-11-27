---
feature: mcp-manage
title: ug mcp (list, remove, add, web-search)
platform: cli
paths: [src/ucode/cli.py, src/ucode/mcp.py, src/ucode/mcp_web_search.py]
status: 'unverified: list, unconfigured remove, web-search --capabilities, and non-interactive add (staging) driven; pickers and configured remove not driven'
---

# ug mcp

Inspect and manage the Databricks MCP servers ug registers with coding agents. Group `mcp_app` `cli.py:2090`; `add` `cli.py:2341`, `remove` `cli.py:2476`, `list` `cli.py:2510`, `web-search` `cli.py:2542`.

## Reach

From a shell, run `ug mcp <subcommand>` with the isolated environment in `../SKILL.md` (Environment).

## Drive

Env: `HOME=$H UG_SKIP_OS_SETTINGS=1`, fresh `H`, stdin `</dev/null`.

1. [verified] `ug mcp` (no subcommand).
2. [verified] `ug mcp list` and `ug mcp list --agents claude`.
3. [verified] `ug mcp remove` and `ug mcp remove --agents claude` (unconfigured).
4. [verified] `ug mcp web-search --capabilities`.
5. [verified] `ug mcp add --help`.
6. [verified, staging workspace] With a cached `databricks auth login` for `$UG_TEST_WORKSPACE` (see SKILL.md Environment): `ug mcp add --workspace $UG_TEST_WORKSPACE --location system.ai --names system.ai.dbsql --agents cursor`, then `ug mcp list`.
7. [verified, staging workspace] `ug mcp add --workspace $UG_TEST_WORKSPACE --location system.ai --names system.ai.dbsql` (no `--agents`, fresh HOME).
8. [unverified: interactive] `ug mcp add` without `--location` on a TTY opens a picker. An explicit `--workspace` that differs from the saved one is refused (`cli.py:2415-2428`). `--agents` naming an unconfigured model agent triggers that agent's setup and a forced login; not driven.
9. [unverified: interactive] `ug mcp remove` with servers configured opens a picker (`--agents` limits it).

## Expect

- 1: exit 2, help with commands add, remove, list, web-search.
- 2: exit 0, heading `MCP servers`, `Workspace: not configured`, "No MCP servers are configured by ug." (or "...for claude.").
- 3: exit 1, `ERROR Workspace is not configured. Run `ug configure` first.`, no files.
- 4: exit 0, one JSON line: `{"external_provider_contract": 1, "provider_env": "UCODE_CLAUDE_WEB_SEARCH_PROVIDER", "managed_entry_flag": "--managed-by-ucode", "automatic_provider": "external-if-safe"}`. No files.
- 5: exit 0, option list (--location, --names, --agents, --workspace, --profile).
- 6: exit 0, "Databricks auth already available", "Configuring for: Cursor", "Saved". It does not force a browser login. `.cursor/mcp.json` gains server `system-ai-dbsql` whose command is `<ug on PATH> mcp-proxy --url <ws>/ai-gateway/mcp-services/system.ai.dbsql --host <ws>` (no tokens), and `.ucode/state.json` is written. `ug mcp list` then shows `system-ai-dbsql  system.ai.dbsql  cursor  connected`.
- 7: exit 1, "No MCP clients are configured for <ws>. Pass --agents ...".

## Gotchas

- `ug mcp list` asks every installed agent CLI for `mcp list`. Even with no servers it ran cursor, opencode and claude, which wrote files under HOME (`.claude.json`, `.cursor/cli-config.json`, `.local/share/opencode/*`, `.cache/*`, `.ucode/opencode-xdg/*`). `--agents claude` limits that to `.claude.json` plus a backup. All stay inside the temp HOME.
- `ug mcp web-search` without `--capabilities` serves MCP over stdio and blocks; do not run it casually.
- `remove` does not need a Databricks login but needs a saved workspace.
- The saved proxy command is whichever `ug` is first on PATH (here an older `~/.local/bin/ug`), not the binary you ran. Check versions when verifying proxy behavior.
