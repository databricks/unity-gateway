# Claude mod entry point

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
launch-scoped `ug-smart-router` plugin. Claude Code 2.1.290 or newer is required.

`ucode.agents.claude_mod.write_hooks` copies the packaged TypeScript entry point
and writes `hooks/hooks.json` into that plugin. Both full and subagent-only smart
routing launches use this path, and the existing temporary-directory lifecycle
cleans up the files when Claude exits.

The initial `session.start` handler forwards the event to Claude. Smart Router
request metadata and native `/smart-router on|off` behavior are added in the next
PR in the stack.

The `Claude mod · 2.1.290` CI job validates the packaged module and runs its
TypeScript event-forwarding test. To run those checks locally:

```bash
UCODE_TEST_CLAUDE_BINARY=/absolute/path/to/claude \
UCODE_TEST_CLAUDE_VERSION=2.1.290 \
uv run pytest tests/native_claude/test_mod.py -q
```

See [Claude Mods](https://code.claude.com/docs/en/plugins/mods/overview).
