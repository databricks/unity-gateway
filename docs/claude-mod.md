# Claude session routing

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
launch-scoped `ug-smart-router` plugin. Claude Code 2.1.290 or newer is required.

The existing `_write_routed_claude_plugin()` copies the packaged TypeScript entry point
from `src/ucode/agents/claude_mods/` and writes `hooks/hooks.json` into that plugin. Both full and subagent-only smart
routing launches use this path, and the existing temporary-directory lifecycle
cleans up the files when Claude exits.

The mod merges the launch recipe into `CLAUDE_CODE_EXTRA_BODY` and intercepts the
existing skill's `/smart-router on|off` command locally, without inference or command
registration. Off sets every routing flag to `0` and the recipe to `DISABLED`; on
restores the original flags and recipe, including originally absent variables.
Unrelated request fields are preserved. A failed write attempts to restore prior values
and reports failure; the native environment API does not provide an atomic transaction.

The baseline comes from native plugin options and survives `/clear`, `/compact`, and
plugin reloads. Toggles affect future requests and newly spawned hooks; already running
child processes retain their inherited environment. No toggle writes settings or snapshots.

The `Claude mod · 2.1.290` CI job validates the packaged module, runs its TypeScript
unit tests, and checks native sessions against a local fixture API. It does not establish
live gateway or interactive TUI coverage. To run these component checks locally:

```bash
UCODE_TEST_CLAUDE_BINARY=/absolute/path/to/claude \
UCODE_TEST_CLAUDE_VERSION=2.1.290 \
uv run pytest tests/native_claude/test_mod.py -q
```

UG passes the resolved routing flags and recipe in the existing launch settings under
`pluginConfigs["ug-smart-router"].options`. Claude supplies these to `register(on, options)`.
`unset` lists originally absent flags because native options cannot carry null values.
The source remains static; no additional launch snapshot file is created.

Claude no longer creates a session-override file. Its hooks read the environment changed by
the mod, including orchestration. The existing skill remains installed; its Claude fallback
asks for a restart if the mod is unavailable. Codex retains its existing file-based controls.
`ug claude --enable-smart-routing/--disable-smart-routing` controls a new launch;
use `/smart-router on|off` to change the running Claude session.

See [Claude Mods](https://code.claude.com/docs/en/plugins/mods/overview).
