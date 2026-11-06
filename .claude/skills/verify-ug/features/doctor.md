---
feature: doctor
title: ug doctor
platform: cli
paths: [src/ucode/doctor.py, src/ucode/cli.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug doctor

Diagnose the local setup (uv, npm, Databricks CLI, workspace, agent CLIs) and offer fixes. Command `src/ucode/cli.py:5461`, logic `src/ucode/doctor.py:297`.

## Reach

From a shell, run `ug doctor` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. `H=$(mktemp -d); HOME=$H UG_SKIP_OS_SETTINGS=1 ./.venv/bin/ug doctor </dev/null`

## Expect

- Exit 0 (also when issues are found; `doctor()` always returns 0, `doctor.py:325`).
- Lines per check: `uv: found on PATH`, `npm: found on PATH`, `Databricks CLI: ...`, `Workspace: not configured`, `ug: v<version> (installed from GitHub)`, then `N issues found; 0 fix(es) applied.`.
- Observed with a fresh HOME where the `databricks` wrapper on PATH couldn't run: Databricks CLI `installed, but its version could not be read`, so 2 issues.
- Writes under HOME: only a cache file from the local `databricks` wrapper. No state.json.

## Gotchas

- Offline when no workspace is saved. With a saved workspace it runs `check_databricks_auth` (`doctor.py:214`), which calls the Databricks CLI and may hit the network (unverified, not driven).
- Fix prompts use `prompt_yes_no_default(..., default=False)`; with stdin closed no fix is applied. A missing Databricks CLI or agent CLI would offer an install (network), so keep stdin closed.
- Configured agents add one check each (`_check_agent_clis`, `doctor.py:154`) and may run the agent's `--version`; not driven.
- Output is Rich text with no `--json`.
