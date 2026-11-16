---
feature: status
title: ug status
platform: cli
paths: [src/ucode/cli.py, src/ucode/state.py, src/ucode/state_file.py, src/ucode/managed_config.py]
status: 'unverified: partial - unconfigured and offline-seeded states driven; live model refresh and managed-config branches not driven'
---

# ug status

Show workspace, per-agent config, models, MCP/skill counts. Handler `status()` at `src/ucode/cli.py:1802`, command at `cli.py:5439`.

## Reach

From a shell, run `ug status` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. [verified] Unconfigured: `ug status`.
2. [verified] Seeded: `mkdir -p $H/.ucode`, write `$H/.ucode/state.json` =
   `{"state_version":3,"current_workspace":"https://example.cloud.databricks.com","workspaces":{"https://example.cloud.databricks.com":{"available_tools":["claude"]}}}`
   (layout: `src/ucode/state_file.py:10-80`), then `ug status`.
3. [unverified: needs workspace] Configured with `profile` saved: triggers live managed-config and model discovery (`cli.py:1713,1772`), which authenticate.
4. [unverified: not seeded] Workspace-managed branch ("Workspace-managed (cached)"): needs the managed-config cache plus state; export recipe seeds the cache only.

## Expect

- Step 1: exit **0** (not 1), output "Not Configured" badge, Provider panel `Workspace URL: not configured`, `Configuration: Self-configured`, "No coding agents are configured." No files written.
- Step 2: exit 0. Warnings "Live managed configuration needs the CLI profile saved by ug configure." and "Live model discovery needs..." (no network because no `profile`), badge "Configured", Provider panel with the workspace URL, a "Claude Code" panel: Model provider "Databricks AI Gateway", "Models (cached): none available", MCP servers 0, Skills 0, Endpoint `https://example.cloud.databricks.com/ai-gateway/anthropic`. state.json unchanged.

## Gotchas

- Unconfigured status exits 0; exit 1 only on RuntimeError.
- Do NOT add a `profile` key to the seeded state: status then calls Databricks for a token and models.
- A `databricks` wrapper that depends on the real HOME can break under a fresh HOME (see SKILL.md Environment); status itself did not need the CLI.
- Output is Rich (panels); there is no `--json`.
