---
feature: version-help
title: Version and help output
platform: cli
paths: [src/ucode/cli.py, src/ucode/telemetry.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Version and help

Print the ug version, the grouped top-level help, and per-subcommand help.

## Reach

Binary: `./.venv/bin/ug` (run `uv sync` if missing).
Every drive: `H=$(mktemp -d); HOME=$H UG_SKIP_OS_SETTINGS=1 <ug> ...`, stdin `</dev/null`, argv words separate.

## Drive

1. [verified] `ug --version` and `ug -V` (root callback `default`, `src/ucode/cli.py:4355`, eager option).
2. [verified] `ug --help`.
3. [verified] `ug <cmd> --help` for any subcommand, e.g. `ug status --help`, `ug claude --help`, `ug mcp`, `ug agents --help`.
4. [verified] `ug bogus`.
5. [verified] `ug --header bad status` (global option validation).

## Expect

- Step 1: exit 0, one line `0.2.1+83.gf83f1c65` (version derived from git describe; value changes with the commit).
- Step 2: exit 0. Rich panels in order: Launch (claude codex copilot cursor gemini opencode kilo pi antigravity), Setup (configure), Tools and Skills (mcp skills agents), Manage (export revert status upgrade doctor), Usage (usage web), then Global Options (`--version/-V --dry-run --skip-preflight --workspace --header --help`). Panel order comes from `_HELP_COMMAND_ORDER` (`cli.py:2000` area) and `rich_help_panel=`.
- Step 3: exit 0, `Usage: ug <cmd> [OPTIONS]` plus an Options panel.
- Step 4: exit 2, "No such command 'bogus'."
- Step 5: exit 1, `ERROR --header #1: expected \`Name: value\`...`.
- No files written under HOME in any of these.

## Gotchas

- Hidden commands (mcp-proxy, auth-token, otel-*, *-router-hook, cowork-hook, claude-desktop-auth, smart-router-describe) are absent from `--help` but answer `<cmd> --help`. See hidden-helpers.
- `mcp-proxy --help` is argparse style (`usage: ... [-h]`), not Rich.
- `ug mcp` with no subcommand prints help but exits 2.
- Bare `ug` (no args) runs the default configure/launch flow; not driven here (unverified), do not run without a workspace.
- Help text wraps at 80 columns; long option help is truncated with an ellipsis in narrow panels.
