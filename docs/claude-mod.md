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

The same entry point routes Agent/Task calls in a native `tool.call` hook. It pipes the
call as JSON to `ug claude-router-hook route-subagent-mod`, whose argument list arrives in
the `routeCommand` plugin option, and continues the call with the returned input. The
matching Agent row shows a spinner and elapsed seconds while the router answers, then the
selected model, routing time, and rationale. Results live in memory by `tool_use_id`, so a
plugin reload drops them. Disabled routing, command failures, and invalid responses keep
the original call; failures also log a diagnostic.

The old routing PreToolUse hook is no longer written, so a call is not routed twice. A
`route-subagent` hook that an earlier UG version persisted still gets its PreToolUse
response and plain-text notice.

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
before `/clear` still survives it. The live skill-toggle journey covers the real CLI write;
native component tests stub the process calls.

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

## Savings estimate

`hooks/register.ts` draws a row above the prompt, styled like the status band: the
estimated savings from smart routing, with the amount in a pill (green for a saving,
yellow when routing cost more), and, when the launch activates Smart Router Orchestrator,
the skill's `metadata.version` from `SKILL.claude.md`, labeled
`Unity Gateway Smart Router v<version>`. The row appears only once there is an estimate,
and then takes the status band's place, so the two never stack; the prompt hint row still
shows the routing state. Its handler decides before running the other `AbovePrompt`
handlers, so the band steps aside whichever order Claude runs them in. For each turn
request, main agent and subagents, the mod sums token usage by the model that served it
and its baseline. Main is its own baseline, and a subagent's baseline is the main model
in effect when it ran.
Under first-prompt routing, requests before the user changes the main model are priced
against the model the session started on.

After each turn the mod writes those sums to `savings_usage`, a file in the launch's
temporary directory. It then runs the `savings_pricer` argv
(`python -P -m ucode.smart_routing.savings`), which prices the sums from the
workspace's cached endpoint rates and prints the estimate's parts (direction, amount,
percent) as one JSON line. Each savings-enabled routed launch
refreshes that cache on a background thread. An estimate is hidden, not undercounted,
when any served or baseline model has no rate. The orchestrator version is passed
as `orchestrator_version`.

Savings hooks live in `hooks/register.ts` alongside the routing controls: Claude
allows one hook module per plugin and its capability scanner follows engine API
calls only within that module. The shared `session.start` handler captures the
start model before first-prompt routing. Savings handlers are registered only
when the launch options explicitly enable the feature.

The entire savings mod requires `ENABLE_SMART_ROUTING_SAVINGS=1`. Otherwise no usage
is tracked or written, no endpoint rates are fetched, no pricer runs, and no savings row is
shown; the three options stay at their declared defaults.
All existing versioned configurations set the flag to `"0"`. Select
`SMART_ROUTER_CONFIG_VERSION=subagent_orch_savings_v0` to enable it with subagent
routing and orchestration. Session routing off/on also disables/restores savings
from the launch baseline.
