# Claude mod entry point

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
launch-scoped `ug-smart-router` plugin. Claude Code 2.1.290 or newer is required.

The existing `_write_routed_claude_plugin()` copies the packaged TypeScript entry point
from `src/ucode/agents/claude_mods/` and writes `hooks/hooks.json` into that plugin. Both full and subagent-only smart
routing launches use this path, and the existing temporary-directory lifecycle
cleans up the files when Claude exits.

The mod merges the launch recipe into the `Databricks-Smart-Router-Recipe` header
via `ANTHROPIC_CUSTOM_HEADERS` and intercepts the existing skill's `/smart-router on|off` command locally, without inference or command
registration. Off sets every routing flag to `0` and the recipe to `DISABLED`; on
restores the original flags and recipe, including originally absent variables.
Unrelated headers are preserved, and `CLAUDE_CODE_EXTRA_BODY` is left untouched.
Duplicate recipe headers are replaced case-insensitively. Recipe line breaks are rejected.
A failed write attempts to restore prior values and reports failure; the native environment API does not provide an atomic transaction.

If a caller supplies `smart_router_recipe_name` in the request body, gateways with
body-recipe parsing enabled give it precedence over the header; toggles preserve that override.

The baseline comes from native plugin options and survives `/clear`, `/compact`, and
plugin reloads. Toggles affect future requests and newly spawned hooks; already running
child processes retain their inherited environment. No toggle writes settings or snapshots.

An explicit `/smart-router on|off` also asks UG's CLI to replace the session controls that
routing hooks apply over the environment, so an "off" saved earlier by the CLI or the skill
cannot outlast the command. The mod writes no file itself: it runs the existing
`--enable-smart-routing`/`--disable-smart-routing` entry point, which validates and replaces
that file atomically. Session start restores only the launch baseline, so an override saved
before `/clear` still survives it. The plugin test host provides no process capability, so
this path is covered by the live skill-toggle journey rather than `claude plugin test`.

The existing Claude skill-toggle integration journey checks recorded inference
headers during its three calculation phases: launch recipe, `DISABLED` after off,
and launch recipe after on. Each phase retains completed native parent/child
answers and requires successful real gateway responses.

The `Claude mod · 2.1.290` CI job validates the packaged module and checks event
forwarding and launch-local options. To run those component checks locally:

```bash
UCODE_TEST_CLAUDE_BINARY=/absolute/path/to/claude \
UCODE_TEST_CLAUDE_VERSION=2.1.290 \
uv run pytest tests/native_claude/test_mod.py -q
```

See [Claude Mods](https://code.claude.com/docs/en/plugins/mods/overview).

UG passes the resolved routing flags and recipe in the existing launch settings under
`pluginConfigs["ug-smart-router"].options`. Claude supplies these to `register(on, options)`.
`unset` lists originally absent flags because native options cannot carry null values.
The source remains static; no additional launch snapshot file is created.
