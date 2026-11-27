---
feature: export
title: ug export
platform: cli
paths: [src/ucode/managed_export.py, src/ucode/managed_config.py, src/ucode/managed_setup.py, src/ucode/cli.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug export

Print (or write with `-f/--file`) the locally stored managed coding-agent config as portable JSON. Offline, read-only (`src/ucode/managed_export.py:1-12`, command `cli.py:5411`).

## Reach

From a shell, run `ug export` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. Unconfigured: `ug export`.
2. Seed the user cache (works with `UG_SKIP_OS_SETTINGS=1`, `managed_config.py:61,757-769`):
   `mkdir -p $H/.ucode $H/out` and write `$H/.ucode/managed-config.json` =
   `{"workspace":"https://example.cloud.databricks.com","config": <contents of tests/fixtures/managed_config/claude_model_picker.json>}`.
   Python one-liner: `json.dump({"workspace":W,"config":json.load(open(fixture))}, open(path,"w"))`.
3. `ug export` (stdout).
4. `ug export -f $H/out/e.json`.
5. `ug export -f $H/nodir/e.json` (parent missing).
6. Seed with `claude_mps.json` instead (no default_model).

## Expect

- 1: exit 1, `ERROR No managed configuration found locally. Run \`ug configure\`...`; no files written.
- 3: exit 0, JSON on stdout: `workspace`, `spec_version: 1`, `default_agent: CODING_AGENT_CLAUDE_CODE`, `enabled_agents[...]`. Server-owned fields are stripped.
- 4: exit 0, stdout empty, file `e.json` has the same JSON. Only new file is the target.
- 5: exit 1, "Cannot write ...: its parent directory does not exist. Create it first".
- 6: exit 1, "The managed config is not valid... default_agent 'claude' must have a non-empty model_config.default_model." Not every fixture exports; pick one with `default_model`.

## Gotchas

- Needs no state.json; the workspace comes from the cache file when state has none.
- Fixtures without `default_model` (claude_mps.json) fail validation.
- stdout is clean JSON; errors go to stderr.
