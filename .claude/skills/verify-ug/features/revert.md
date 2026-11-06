---
feature: revert
title: ug revert
platform: cli
paths: [src/ucode/cli.py, src/ucode/managed_files.py, src/ucode/managed_config.py, src/ucode/agents/claude.py, src/ucode/agents/codex.py, src/ucode/agents/vscode.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug revert

Clear ug state and restore backed-up agent config files, including machine-wide managed settings. Command `src/ucode/cli.py:5449`, `revert()` at `cli.py:1921`.

## Reach

From a shell, run `ug revert` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. `H=$(mktemp -d); HOME=$H UG_SKIP_OS_SETTINGS=1 ug revert </dev/null` (fresh HOME only).

## Expect

- Exit 0. Heading `Revert`, `Workspace: none`, one `<agent> config: unchanged` line per agent, OS-managed lines `unchanged`, `Claude Desktop profile: unchanged`, `VS Code extension: unchanged`, one `<client> MCP config: unchanged` per MCP client, then `ug state cleared`.
- Two warnings on this box because `/etc/claude-code/managed-settings.json` and `/etc/codex/managed_config.toml` exist: "left unchanged because no UG backup record was found".
- Writes: only `$H/.ucode/state.json`. Mtimes of the two /etc files were unchanged after the run.

## Why it is safe under UG_SKIP_OS_SETTINGS=1 (read from code)

- `request_explicit_managed_writes` (`managed_files.py:370`) makes managed writes mandatory, but `revert_managed_file` (`managed_files.py:851`) returns "unchanged" without a manifest entry, and the temp HOME has no `managed-backups/manifest.json`.
- `claude_header_mod_root()` returns None when skipped (`managed_files.py:197`), so no `sudo rm -rf` of `/etc/claude-code/ucode-plugins`.
- `revert_machine_policy` returns "unchanged" when `_machine_policy_installable()` is false (`managed_config.py:995`).
- VS Code revert only runs `code` when an extension folder exists under HOME (`agents/vscode.py:296`).

## Gotchas

- Without UG_SKIP_OS_SETTINGS=1, or with a seeded manifest, revert can run `sudo` against /etc. Never drive that variant. Not driven: reverting a configured state (needs a prior configure).
- Exit code is the `revert()` return value; failure paths (e.g. Desktop revert "failed") are not driven.
