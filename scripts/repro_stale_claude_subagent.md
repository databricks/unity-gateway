# Claude route-agent loss on plugin refresh

Confirmed on Claude Code **2.1.248**, using the CLI agent registration mechanism
in Unity Gateway **0.1.0+9858a84**. The diagnostic launches real Claude directly,
with a small routing hook and inherited child models. It does not exercise Isaac,
the live smart-router service, or GLM inference.

## Reproduce

Run from this checkout with a configured Unity Gateway workspace. The script
reuses the active workspace's Claude authentication helper and model aliases.
It creates a disposable working directory and Claude config directory; machine
managed settings still apply. Each experiment makes real model requests.

```sh
uv run python scripts/repro_stale_claude_subagent.py \
  --interactive --registration cli \
  --claude /path/to/claude/versions/2.1.248 --log-dir /tmp/route-refresh-cli
```

Complete the visible onboarding and trust prompts, then enter these in order:

1. `Use one Explore subagent to reply with exactly BEFORE_RELOAD. Wait for its result.`
2. After the child finishes: `/reload-plugins --force`
3. After `Reloaded:` appears: `Use one Explore subagent to reply with exactly AFTER_RELOAD. Wait for its result. If it fails, report the tool error and do not retry.`
4. `/exit`

Expected on 2.1.248: the first routed child completes; the second reports
`Agent type 'ucode-route-glm-5-3-982d9f93' not found`. The script checks actual
tool results, child completion notifications, and routing evidence in the debug
log. Exit 1 means reproduced, 0 means both calls completed across refresh, and
2 means inconclusive. Existing log directories must not be reused for a new run.

Repeat with `--registration plugin` and a different log directory. Expected:
both routed children complete because plugin refresh reloads their definitions
from disk. This tests the registration strategy used by PR #779; the production
plugin's exact model mappings are covered separately by launcher unit tests.

Restart existing Claude sessions after upgrading UG to this change: the hook's
new plugin-qualified agent names must match the definitions registered at launch.
Upgrading the hook executable alone does not update an already-running registry.

## Recorded result

On 2026-09-22, both experiments ran against the actual 2.1.248 interactive TUI:

| Registration | Before refresh | After refresh |
| --- | --- | --- |
| CLI `--agents` | Child completed | Exact missing route-agent error |
| Plugin `--plugin-dir` | Child completed | Child completed |

The CLI run logged a routed completion at `03:37:27.451Z` and
`refreshActivePlugins` at `03:37:39.517Z`, followed by the missing-agent tool
result. The plugin run logged completions at `03:39:01.859Z` and
`03:40:03.140Z`, with refresh at `03:39:50.031Z` between them.

The failure needs neither nested agents nor parallel calls. Claude's interactive
plugin refresh replaces its registry without retaining CLI-defined agents;
the headless refresh path preserves them. Ten earlier headless parallel-spawn
trials did not reproduce the issue.

This establishes a causal reproduction, not the trigger in the original user's
session. Check that session's debug logs for `Auto-refreshing plugins` or
`refreshActivePlugins` before the first missing-agent error to connect it to
automatic plugin refresh.

## Analyze saved evidence without inference

```sh
uv run python scripts/repro_stale_claude_subagent.py \
  --registration cli --analyze-reload /tmp/route-refresh-cli/cli-01 --exit-code 0
```

Supply the known Claude process exit code, not the diagnostic script's exit
code. For the plugin run use `--registration plugin` and its `plugin-01`
directory. Logs remain local and can contain workspace details; do not publish
raw settings, credentials, or unreviewed debug logs.
