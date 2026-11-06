---
name: verify-ug
description:
  Feature map for Unity Gateway (`ug`): how to reach, drive, and check each
  user-facing CLI feature. Use whenever you're verifying a change to ug — before
  calling a task done, after editing commands, configure/launch behavior, MCP or
  skills management, or when asked to test or QA a feature — so you drive the
  real behavior instead of guessing.
---

# Verify ug

Unity Gateway is a Python CLI (`ug`, package `src/ucode/`) that configures and
launches coding agents through Databricks AI Gateway. Verify by running this
checkout's binary inside a throwaway HOME so nothing touches your real agent
config or `/etc`.

## Environment

- Start: from the repo root, `uv sync` creates the venv. Set `UG` to the absolute
  path of `.venv/bin/ug` (ready when `"$UG" --version` prints `0.2.1+<n>.g<sha>`).
  Don't rely on a `ug` on PATH, which may be an older install.
- Base: every run gets `H=$(mktemp -d)` and
  `HOME=$H UG_SKIP_OS_SETTINGS=1 timeout 120 "$UG" ... </dev/null`. Never use sudo.
  `UG_SKIP_OS_SETTINGS=1` keeps ug away from `/etc/claude-code` and `/etc/codex`.
- Inside a smart-routed agent session, unset the inherited routing env first, or
  child runs inherit it: `env -u ANTHROPIC_BASE_URL -u CLAUDE_CODE_USE_GATEWAY
  -u UG_HEADERS -u ENABLE_SMART_ROUTING_V2 -u ENABLE_SMART_ROUTING_SUBAGENT_ONLY
  -u ENABLE_SMART_ROUTER_ORCHESTRATOR -u SMART_ROUTER_CONFIG_VERSION
  -u SMART_ROUTER_NAME -u UCODE_SESSION_ENV_FILE -u UCODE_SMART_ROUTER_PYTHON
  -u UG_AUTO_UPGRADE_AGENT -u UG_LAUNCH_WRITE_OS_SETTINGS`.
- Databricks CLI: if the `databricks` on PATH is a wrapper that depends on files under
  the real HOME, it fails under a fresh HOME ("Could not parse Databricks CLI
  version"). Put a directory first on PATH with a `databricks` shim that execs the
  real CLI binary.
- Auth (workspace features only): a non-production workspace in `$UG_TEST_WORKSPACE`,
  logged in once with `databricks auth login --host $UG_TEST_WORKSPACE` under the real
  HOME. Symlink `$H/.databricks` and `$H/.databrickscfg` to the real ones. Never
  print their contents or run `ug auth-token`.
- Seed data: offline state, skills-manifest and managed-config recipes are in
  `features/status.md`, `features/skills-manage.md` and `features/export.md`.
  `UCODE_MANAGED_CONFIG_STUB=<json file>` replaces the managed-config fetch; a file
  containing `null` means "none published".
- Drivers: cli

## Verifying a change

1. Find entries whose `paths` overlap your diff:
   `git diff --name-only <base>...HEAD`, then match against each entry's `paths`
   below. `src/ucode/cli.py` is in almost every entry, so for cli.py changes match by
   the command you touched.
2. Read those feature files and drive them, following the steps exactly.
3. Update each entry you drove, and only those:
   - it worked → set `verified: <sha> <date>`, fix any step you had to adjust,
     and record what you actually observed;
   - it failed because of your change → fix your change, not the entry;
   - it failed because the entry is out of date → correct the steps from what
     you observed, or set `unverified: failed at step <N> on <sha>`.
4. If your change added a feature, add an entry for it using the same format.
5. If you needed a feature with no entry, say that the feature map could be
   updated for it.

Record what you observed, never what the code implies. Steps carry their own
`[verified]` / `[unverified: reason]` tags; an `unverified` entry may still have
many verified steps.

## Features

| Feature | File | Paths | Status |
| --- | --- | --- | --- |
| Version and help | [features/version-help.md](features/version-help.md) | `src/ucode/cli.py` | verified `f83f1c65` |
| Configure an agent | [features/configure-agent.md](features/configure-agent.md) | `cli.py`, `managed_*.py`, `agents/__init__.py` | unverified (validation only) |
| Launch Claude Code | [features/launch-claude.md](features/launch-claude.md) | `cli.py`, `launcher.py`, `agents/claude.py` | unverified (partial) |
| Launch Codex | [features/launch-codex.md](features/launch-codex.md) | `launcher.py`, `agents/codex*.py` | unverified (`--version` only) |
| Launch other agents | [features/launch-other-agents.md](features/launch-other-agents.md) | `agents/{gemini,opencode,kilo,copilot,pi,antigravity,cursor}.py` | unverified (partial) |
| Smart routing | [features/smart-routing.md](features/smart-routing.md) | `src/ucode/smart_routing/**`, `constants.py` | unverified (partial) |
| Status | [features/status.md](features/status.md) | `cli.py`, `state*.py`, `managed_config.py` | unverified (partial) |
| Export managed config | [features/export.md](features/export.md) | `managed_export.py`, `managed_config.py` | verified `f83f1c65` |
| Open workspace in browser | [features/web.md](features/web.md) | `src/ucode/cli.py` | verified `f83f1c65` |
| Doctor | [features/doctor.md](features/doctor.md) | `src/ucode/doctor.py` | verified `f83f1c65` |
| Revert | [features/revert.md](features/revert.md) | `managed_files.py`, `agents/{claude,codex,vscode}.py` | verified `f83f1c65` |
| Usage and spend limits | [features/usage.md](features/usage.md) | `usage.py`, `budget.py` | verified `f83f1c65` |
| Manage MCP servers | [features/mcp-manage.md](features/mcp-manage.md) | `mcp.py`, `mcp_web_search.py` | unverified (partial) |
| Manage skills | [features/skills-manage.md](features/skills-manage.md) | `skills_*.py` | unverified (partial) |
| Manage agents | [features/agents-manage.md](features/agents-manage.md) | `cli.py`, `state.py` | unverified (partial) |
| Hidden helpers | [features/hidden-helpers.md](features/hidden-helpers.md) | `cli.py`, `smart_routing/` | unverified (partial) |
| Claude Desktop, Cowork and Code plugins | [features/desktop-integration.md](features/desktop-integration.md) | `src/ucode/agents/desktop/**` | unverified (static) |
| VS Code Copilot | [features/vscode-copilot.md](features/vscode-copilot.md) | `agents/vscode.py`, `vscode-extension/**` | unverified (static) |
| Upgrade | [features/upgrade.md](features/upgrade.md) | `src/ucode/cli.py` | unverified (not run by design) |

Verified: 6/19

## Findings

- Root `ug --dry-run <command>` doesn't make subcommands dry: `set_dry_run` runs
  after the subcommand early-return. Fix: set it before dispatch
  (`src/ucode/cli.py:4397-4400`; `ug --dry-run claude -- --version` printed
  "Starting Claude Code", exit 0).
- `configure`, `agents add`, `--profile` and `--workspace` force a browser login even
  with a valid cached token, so configure can't be driven headlessly. Fix: try the
  cached token before `force_login` (`cli.py:737` `force_login=not use_pat`; exit 124 at
  the OIDC prompt while `databricks current-user me` succeeded).
- `configure --user` is refused for claude/codex when `/etc` managed files exist, so
  user-scope config can't be verified on a machine that already has them. Fix: an
  isolation override for the managed-file paths (`agents/__init__.py:529-552`).
- A managed-config provider cached in `state.json` survives a later
  `UCODE_MANAGED_CONFIG_STUB`; only a fresh HOME clears it (receipt: provider error
  persisted after setting the stub).
- Saved proxy/auth commands use whatever `ug` is first on PATH, not the running binary,
  so a venv-driven check silently mixes versions (`.cursor/mcp.json` and
  `auth_command` pointed at `~/.local/bin/ug` 0.1.0).
- Skills commands probe the Databricks CLI before any logic, so even `skills list` fails
  in a fresh HOME. Fix: skip the probe for `list`/`remove` (`cli.py:2590,2798`).
- No machine-readable output for status, doctor, revert, agents/skills/mcp list
  (Rich only); only `smart-router-describe` and `mcp web-search --capabilities` emit
  JSON. Fix: a `--json` flag.
- `ug status` exits 0 when unconfigured; only a RuntimeError exits 1
  (`cli.py:5439-5446`). Scripts can't use the exit code to detect "not configured".
- `ug mcp list` runs every installed agent CLI's own `mcp list`, which writes into
  HOME. Fix: an opt-out or read-only mode.
