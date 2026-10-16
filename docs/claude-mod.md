# Claude mod entry point

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
launch-scoped `ug-smart-router` plugin. Claude Code 2.1.290 or newer is required.

The existing `_write_routed_claude_plugin()` copies the packaged TypeScript entry point
from `src/ucode/agents/claude_mods/` and writes `hooks/hooks.json` into that plugin.
Every routed launch includes the status band above the prompt. It shows at launch and after
each `/smart-router on|off`, and hides once the next prompt's turn completes. The prompt hint row (the line under the
input with the permission mode) always ends with `smart router ON` or `smart router OFF`. Both full and subagent-only smart
routing launches use this path, and the existing temporary-directory lifecycle
cleans up the files when Claude exits.

The same entry point intercepts Agent/Task calls through native `tool.call` hooks.
It runs `ug claude-router-hook route-subagent-mod` with a JSON payload on stdin.
Python uses the same routing API client as Codex and returns the updated input,
model, and rationale as JSON on stdout. The mod keeps that result in memory by
`tool_use_id` and renders the model and rationale under the matching Agent row.
It writes no routing sidecar file. Disabled routing or an unavailable route leaves
the call unchanged; command failures and invalid responses also keep the original
input and show a diagnostic. Other mods and Claude's permission checks still run.

Launch settings pass the command's argument list through the native `routeCommand`
plugin option, including the workspace, profile/PAT selection, and offered model IDs.
SessionStart/SubagentStart audit hooks remain; the old routing PreToolUse hook is
removed so the same call cannot be routed twice. A `route-subagent` hook that an earlier
UG version persisted still gets its PreToolUse response and plain-text notice. UI details last for the loaded
mod's lifetime; a plugin reload does not restore earlier routing annotations.

`constants.ts` holds the values the mod shares with UG's CLI and the gateway: the recipe
body field, the disabled recipe, the CLI module, agent, and routing flags, and the
routing keys used to read session overrides. It ships
beside the entry point and stays out of `hooks.json`, which lists only `register.ts` as a
module. Claude's
capability scanner requires a literal name at each `$.env.get`/`$.env.set` call site so it
can list what the module reads and writes, and `plugin validate` rejects a variable in that
position: "$.env.get takes a literal name as its first argument". Keep those names inline.

The mod merges the launch recipe into `CLAUDE_CODE_EXTRA_BODY` as
`smart_router_recipe_name` and intercepts the existing skill's `/smart-router on|off` command locally, without inference or command
registration. Off sets every routing flag to `0` and the recipe to `DISABLED`; on
restores the original flags and recipe, including originally absent variables.
Other request-body fields are preserved. The mod leaves `ANTHROPIC_CUSTOM_HEADERS`
untouched, and UG no longer adds a recipe header to Claude's settings.
Recipe line breaks and extra bodies that are not JSON objects are rejected.
A failed write attempts to restore prior values and reports failure; the native environment API does not provide an atomic transaction.

The launch and explicit toggles replace any existing `smart_router_recipe_name` body field.
The gateway must support body-recipe parsing.

The baseline comes from native plugin options and survives `/clear`, `/compact`, and
plugin reloads. Toggles affect future requests and newly spawned hooks; already running
child processes retain their inherited environment. No toggle writes settings or snapshots.

An explicit `/smart-router on|off` also asks UG's CLI to replace the session controls that
routing hooks apply over the environment, so an "off" saved earlier by the CLI or the skill
cannot outlast the command. The mod writes no file itself: it runs the existing
`--enable-smart-routing`/`--disable-smart-routing` entry point, which validates and replaces
that file atomically. Session start restores only the launch baseline, so an override saved
before `/clear` still survives it. The live skill-toggle journey covers the real CLI write; native component tests
use the test host's process stubs to check the subagent routing protocol and display.
A native process check also runs the real disabled Python CLI and verifies inherited
launch flags and a dummy token; it makes no gateway request.

The existing Claude skill-toggle integration journey checks recorded inference
payloads during its three calculation phases: launch recipe, `DISABLED` after off,
and launch recipe after on. Each phase retains completed native parent/child
answers and requires successful real gateway responses, with no recipe header.

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
