# Claude mod entry point

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
launch-scoped `ug-smart-router` plugin. Claude Code 2.1.290 or newer is required.

The existing `_write_routed_claude_plugin()` copies the packaged TypeScript entry point
from `src/ucode/agents/claude_mods/` and writes `hooks/hooks.json` into that plugin.
Every routed launch includes the status band above the prompt. Both full and subagent-only smart
routing launches use this path, and the existing temporary-directory lifecycle
cleans up the files when Claude exits.

`constants.ts` holds the values the mod shares with UG's CLI and the gateway: the recipe
header name, the disabled recipe, the CLI module, agent, and routing flags, and the
routing keys used to read session overrides. It ships
beside the entry point and stays out of `hooks.json`, which lists only `register.ts` as a
module. Claude's
capability scanner requires a literal name at each `$.env.get`/`$.env.set` call site so it
can list what the module reads and writes, and `plugin validate` rejects a variable in that
position: "$.env.get takes a literal name as its first argument". Keep those names inline.

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

## Savings estimate

`hooks/register.ts` draws a row under the status band, styled like it: the estimated
savings from smart routing, with the amount in a pill (green for a saving, yellow when
routing cost more), and, when the launch activates Smart Router Orchestrator, the skill's
`metadata.version` from `SKILL.claude.md`, labeled `Unity Gateway Smart Router v<version>`.
The row goes after the other `AbovePrompt` handlers' output, so it stays under the band
whichever order Claude runs them in. For each turn request, main agent and
subagents, the mod sums token usage by the model that served it and its baseline. Main
is its own baseline, and a subagent's baseline is the main model in effect when it ran.
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
is tracked or written, no endpoint rates are fetched, no pricer runs, and no savings or
orchestrator-version row is shown; the three options stay at their declared defaults.
All existing versioned configurations set the flag to `"0"`. Select
`SMART_ROUTER_CONFIG_VERSION=subagent_orch_savings_v0` to enable it with subagent
routing and orchestration. Session routing off/on also disables/restores savings
from the launch baseline.
