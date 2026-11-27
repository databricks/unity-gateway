---
feature: hidden-helpers
title: Hidden helper commands
platform: cli
paths: [src/ucode/cli.py, src/ucode/smart_routing/]
status: 'unverified: only smart-router-describe and --help of each helper driven'
---

# Hidden helpers

Commands absent from `ug --help` that agents or OS hooks invoke. Do not run them casually: most authenticate, open a network connection or serve stdio.

## Reach

From a shell, run `ug <helper> (hidden from `ug --help`)` with the isolated environment in `../SKILL.md` (Environment).

## Drive

1. [verified] `ug smart-router-describe` (`cli.py:3224`, offline), with `HOME=$H UG_SKIP_OS_SETTINGS=1`.
2. [verified] `ug <helper> --help` for each helper below (exit 0).
3. [unverified: needs auth/network/stdio] Running any other helper.

## Expect

- 1: exit 0, one JSON line `{"schema": 1, "harnesses": ["claude", "codex"]}`; no files.
- 2: usage text. Required options per helper, from the help output:
  - `mcp-proxy` (`cli.py:2830`, argparse style): `--url` required; `--host --profile --use-pat`. Stdio MCP proxy to a Databricks endpoint.
  - `cowork-hook` (`2862`): `--host` and `--server name=URL` required. Cowork SessionStart/UserPromptSubmit hook.
  - `claude-desktop-auth` (`2891`): `--host` and `--profile` required; `--relay --claude-binary`. Prints a Desktop credential, may open a browser login.
  - `auth-token` (`2930`): `--host` optional. Prints a bearer token (apiKeyHelper). Short-circuits on DATABRICKS_BEARER.
  - `otel-proxy` (`3036`): `--host` and `--port` required; `--relay-port`. Long-running OTLP proxy.
  - `otel-headers` (`3068`): `--host --profile --use-pat --force-refresh`. Prints OTLP headers JSON.
  - `codex-router-hook` (`3148`) and `claude-router-hook` (`3234`): positional `event`; `--host --profile --use-pat --model` (claude also `--socket`). Smart-routing lifecycle hooks, read hook JSON on stdin.
  - `agy` (`4829`): hidden alias that launches Antigravity CLI; takes `--provider --model`. Do not run.
- Helpers started on a multi-workspace host also take a hidden `--workspace-id` (AGENTS.md "Isolation").

## Gotchas

- `mcp-proxy --help` is argparse style; the others are Rich/Typer.
- `smart-router-describe` is offline and ignores a workspace's managed config and launch flags (docstring `cli.py:3226`).
- Behavior of the non-describe helpers is from code and help text only.
