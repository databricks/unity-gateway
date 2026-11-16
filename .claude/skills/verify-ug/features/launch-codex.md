---
feature: launch-codex
title: Launch Codex through the gateway (ug codex)
platform: cli
paths: [src/ucode/cli.py, src/ucode/launcher.py, src/ucode/agents/codex.py, src/ucode/agents/codex_catalog.py]
status: 'unverified: only `-- --version` was driven; no session, no --provider, no prompt. Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Launch Codex (ug codex)

Auto-configure if needed, then exec `codex` against the gateway's Responses API (`cli.py:4481`).

## Reach

Same isolated-HOME recipe as `launch-claude` (state created by `ug mcp add --workspace ... --agents cursor`, stub-null managed config).
Options on `ug codex --help`: `--provider`, `--model-location`, `--refresh`, `--skip-preflight`, `--workspace`. There is no `--model` and no smart-routing flag on this command.

## Drive

1. [verified] `ug codex -- --version`
2. [unverified] `ug codex --provider <cat.schema.name>`, `--model-location main.default`, and a prompt argument.

## Expect

- Step 1 exits 0. Output: "Launching Codex with Unity Gateway", "Starting Codex", then `codex-cli 0.166.0`.
- Writes `.codex/ucode.config.toml` in the temp HOME (plus the shared `.ucode/*` files).
- Also printed: `WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp"`. That is Codex's own warning for a HOME under /tmp, not a ug failure.

## Gotchas

- Codex smart routing is gated by env (`ENABLE_SMART_ROUTING_V2` and friends; see `smart-routing`), not a CLI flag.
- `/etc/codex/managed_config.toml` exists on this machine. `ug configure --user` is refused for Codex because of it. A launch just uses whatever files exist.
- Codex native `--version` avoids any model call. Do not send prompts.
