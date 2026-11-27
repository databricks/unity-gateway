---
feature: usage
title: Show spend and budget limits (ug usage)
platform: cli
paths: [src/ucode/usage.py, src/ucode/budget.py, src/ucode/cli.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug usage

Print your Unity Gateway dollars spent and budget limits as meters (`cli.py:5474`, `usage.py`).

## Reach

Needs a workspace in state. Create one with `ug mcp add --workspace $UG_TEST_WORKSPACE --location system.ai --names system.ai.dbsql --agents cursor` in a temp HOME, then run the commands below. `usage` calls `install_databricks_cli()`: it only checks that `databricks` is on PATH and new enough, and installs (brew) only if absent. With a working CLI on PATH nothing was installed.

## Drive

1. `ug usage --help`
2. `ug usage`
3. `ug usage --all`

## Expect

- Step 1 exits 0 and shows one option, `--all` ("Show all applicable budget limits.").
- Step 2 exits 0: "Databricks auth already available", then "Your Unity Gateway spend limits", then up to 5 blocks of `[meter] N% used`, a limit title ("Monthly personal limit (budget <8 hex>)"), and "$X of $Y used · $Z remaining this month". It ends with "Showing 5 of 28 spend limits. Run ug usage --all to see all limits." Shared limits print a meter and title without a dollar line.
- Step 3 exits 0 and prints every limit (28 here).
- Read-only; no files are written beyond the state created in Reach.

## Gotchas

- Without a configured workspace: exit 1, "Workspace is not configured. Run `ug configure` first."
- Counts and values are workspace data and change; do not assert them.
- Zero-threshold limits show 100% used with $0.00 of $0.00.
