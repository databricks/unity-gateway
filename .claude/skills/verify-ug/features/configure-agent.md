---
feature: configure-agent
title: Configure agents for a workspace (ug configure)
platform: cli
paths: [src/ucode/cli.py, src/ucode/managed_files.py, src/ucode/managed_config.py, src/ucode/agents/__init__.py, tests/fixtures/managed_config/**]
status: 'unverified: flag validation driven; the write path is blocked on this machine (forced browser login, plus --user refused by /etc managed files). Checked at f83f1c65f61aeefe2884ad40891efa06ccafe2ae 2026-11-06'
---

# Configure agents (ug configure)

Pick a workspace and agents, log in, discover models, and write each agent's gateway config (`cli.py:4871`).

## Reach

Binary: `./.venv/bin/ug` (the `ug` on PATH was an older 0.1.0 build).
Set `H=$(mktemp -d)`; run every command as `HOME=$H UG_SKIP_OS_SETTINGS=1 ug ...` with `</dev/null` and `timeout`.
If the `databricks` on PATH is a wrapper that depends on files under the real HOME, it fails in a temp HOME; see SKILL.md Environment.
Put a shim named `databricks` that execs the real binary first on PATH.

## Drive

1. [verified] `ug configure --help` lists `--dry-run --agent --agents --workspace --profile --use-pat --provider --model-location --no-vscode --mcp --global --user --verbose`.
2. [verified] Validation errors need no login:
   - `ug --dry-run configure --provider a.b.c` exits 1: `--provider requires --agent or --agents.`
   - `... --agent claude --model-location bad` exits 1: `--model-location must be <catalog>.<schema>`
   - `... --agent claude --provider bad` exits 1: `--provider must be <catalog>.<schema>.<name>`
   - `... --agent claude --provider a.b.c --model-location a.b` exits 1: `cannot be combined`
   - `... --global --user` exits 1: `Use either --global or --user, not both.`
   - `... --verbose x` exits 2: `--verbose must be one of: normal, low.`
3. [unverified] `ug --dry-run configure --agent claude --workspace $UG_TEST_WORKSPACE`.
   Expected: prints config files, writes none. Observed: exit 124 (timeout). It prints the "Databricks Login" panel and an OIDC authorize URL, then waits for a browser.
4. [unverified] `... --user`. Observed for claude and codex: exit 1, "can't be used while Claude Code managed settings exist at /etc/claude-code/managed-settings.json" (Codex: /etc/codex/managed_config.toml). Source: `agents/__init__.py:529`.
5. [unverified] `--agents claude,codex --mcp system.ai.dbsql --no-vscode --verbose low`, real write with `--user`.

## Expect

- Global (default) writes the machine-wide Claude and Codex managed files with sudo, plus user settings. `--global` writes only the machine-wide files. `--user` writes only `~/.claude/ucode-settings.json`, `~/.codex/ucode.config.toml` and the Desktop user marketplace. It is saved in `~/.ucode/` (`managed_files.py:39,167`).
- `UG_SKIP_OS_SETTINGS=1` has the same effect on managed files, but is not saved.
- What `ug claude` auto-writes into a temp HOME (see `launch-claude`): `.claude/ucode-settings.json`, `.ucode/state.json`, `.claude.json`, plus the `smart-router` skills.
- Fields written per agent: see AGENTS.md "Fields ug manages".

## Gotchas

- `configure`, `agents add` and `configure --profile` always run `databricks auth login` (`cli.py:737`, `force_login=not use_pat`) even when a valid token is cached. Only the hidden `DATABRICKS_BEARER` or `DATABRICKS_BEARER_COMMAND` (`databricks.py:1167`) or `--use-pat` skips it. Neither was tried here.
- `ug --skip-preflight configure` still forces login.
- If /etc already holds managed files (as on the machine this was mapped on), `--user` is refused.
- This workspace publishes a managed config (`ug agents list` shows Claude and Codex "admin-managed"). `--user` would be refused for that too (`agents/__init__.py:552`).
- Offline alternative: `UCODE_MANAGED_CONFIG_STUB=<file>` (`managed_config.py:553,577`) replaces the managed-config HTTP read with a JSON file. A file holding `null` means "no managed config". Fixtures are in `tests/fixtures/managed_config/*.json`. Login and model discovery are still live.
- The stub is only read at configure or launch time. State already cached from a real managed config (for example a model provider) is not cleared by setting the stub afterwards. Use a fresh HOME.
- Saved `auth_command` / apiKeyHelper paths use the `ug` on PATH, not the binary you ran.
