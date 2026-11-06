---
feature: web
title: ug web
platform: cli
paths: [src/ucode/cli.py]
status: 'verified: f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# ug web

Open `<workspace>/ml/ai-gateway` in a browser (`src/ucode/cli.py:5488`).

## Reach

From a shell, run `ug web` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. Unconfigured: `ug web`.
2. Seed `$H/.ucode/state.json` as in the status entry, then `ug web`.

## Expect

- 1: exit 1, `ERROR Workspace is not configured. Run \`ug configure\` first.`; no files.
- 2: exit 0, prints `Unity Gateway: https://example.cloud.databricks.com/ml/ai-gateway`, then on this headless box "Could not open a browser. Open the URL above manually."

## Gotchas

- The browser failing is not an error (exit 0). A `BROWSER` helper may write its own log under HOME.
- No network or auth needed; the URL is built from saved state only.
