# Claude mod entry point

UG's smart-routed Claude launcher loads `hooks/register.ts` through the existing
`ug-smart-router` plugin that each routed launch passes with `--plugin-dir`. Claude Code
2.1.290 or newer is required.

The existing `_write_routed_claude_plugin()` copies the packaged TypeScript entry point
from `src/ucode/agents/claude_mods/` and writes `hooks/hooks.json` into that plugin.
Every routed launch includes the status band above the prompt. Once the session has been on, it
shows at launch and after each `/smart-router on|off`, and hides once the next prompt's turn
completes, and the prompt hint row (the line under the input with the permission mode) ends with
`smart router ON` or `smart router OFF`. A session that starts with Smart Router off on demand
(`ENABLE_SMART_ROUTER_ON_DEMAND=1`) shows neither until its first `on`. Both full and subagent-only smart
routing launches use this path. The plugin lives in `~/.ucode/claude-routing-plugin/<digest>/`,
named after its contents, because a subagent-only launch hands the terminal to Claude and
leaves nothing behind to remove it.

The same entry point routes Agent/Task calls in a native `tool.call` hook. It pipes the
call as JSON to `ug claude-router-hook route-subagent-mod`, whose argument list arrives in
the `routeCommand` plugin option, and continues the call with the returned input. The
matching Agent row shows a spinner and elapsed seconds while the router answers, then the
selected model, routing time, and rationale. Results live in memory by `tool_use_id`, so a
plugin reload drops them. While the session file says Smart Router is off, the hook keeps
the original call without starting the router command. Disabled routing, command failures, and
invalid responses also keep the original call; failures also log a diagnostic.

The old routing PreToolUse hook is no longer written, so a call is not routed twice. A
`route-subagent` hook that an earlier UG version persisted still gets its PreToolUse
response and plain-text notice.

`constants.ts` holds the values the mod shares with UG's CLI and the gateway: the recipe
body field, the disabled recipe, the CLI module, agent, and routing flags, and the
routing keys read from the session file's `when_on`. `session-file.ts` parses the session
file. Both ship beside the entry point and stay out of `hooks.json`, which lists only
`register.ts` as a module. Claude's
capability scanner requires a literal name at each `$.env.get`/`$.env.set` call site so it
can list what the module reads and writes, and `plugin validate` rejects a variable in that
position: "$.env.get takes a literal name as its first argument". Keep those names inline.
Claude Code 2.1.290's scanner also follows `$` only into functions declared in the same file,
so each module reads the session file itself and imports only the parser.

The mod merges the launch recipe into `CLAUDE_CODE_EXTRA_BODY` as
`smart_router_recipe_name` and intercepts the existing skill's `/smart-router on|off` command locally, without inference or command
registration. On sets the recipe and off sets `DISABLED`. A session that starts off carries no
recipe until its first `on`, so its requests match a session without Smart Router. The mod
no longer changes the routing flags: hooks read the session file instead.
Other request-body fields are preserved. The mod leaves `ANTHROPIC_CUSTOM_HEADERS`
untouched, and UG no longer adds a recipe header to Claude's settings.
Recipe line breaks and extra bodies that are not JSON objects are rejected.
A failed recipe or session write attempts to restore the prior recipe and reports failure; the native environment API does not provide an atomic transaction.

Session start, once the session has been on, and explicit toggles replace any existing
`smart_router_recipe_name` body field.
The gateway must support body-recipe parsing.

The recipe comes from native plugin options and survives `/clear`, `/compact`, and
plugin reloads. Toggles affect future requests and newly spawned hooks. No toggle writes
settings or snapshots.

An explicit `/smart-router on|off` asks UG's CLI to flip the session file's switch, which the
routing and orchestrator hooks and the mod read. The mod writes no file itself: it runs the existing
`--enable-smart-routing`/`--disable-smart-routing` entry point, which validates and replaces
that file atomically. Session start re-reads the switch, so a toggle made before `/clear`
survives it. The live skill-toggle journey covers the real CLI write;
native component tests stub the process calls and the file read.

Before each on-to-off change, Isaac sessions with
`ISAAC_SMART_ROUTER_FEEDBACK=1` or `true` require a reason of at least 16 characters after
trimming surrounding whitespace. Isaac sets this flag from its
rollout setting or the user's explicit `1`/`0` or `true`/`false` override. As with UG's other
boolean environment flags, `1`, `true`, `yes`, and `on` enable it (case-insensitive,
with surrounding whitespace ignored); other values disable it. It controls Isaac feedback
availability, so UG's routing presets and on/off toggles leave it unchanged.
The native form keeps routing on until Enter accepts a valid reason, then disables
routing and uploads the exact text in the background with `isaac feedback`. The
transcript shows the feedback ID the mod passes with `--feedback-id`; a toast reports
the result. Cancel or Escape leaves routing on and sends nothing. A missing
`isaac` CLI or an unavailable form leaves routing on. A feedback submission failure after a valid
reason leaves routing off and reports the failure in the transcript. The form makes no model requests.

`isaac-feedback.ts` owns this Databricks-specific policy, form, and submission.
`register.ts` binds the routing callbacks before Isaac's command guard, then
handles toggles the guard passes through. Claude's mod compiler requires engine
API calls to stay in the file that receives `$`.

The CLI/skill path, including Codex, enforces the same minimum through the session
toggle's `--smart-router-feedback` argument. The skill asks for the reason before
retrying the toggle and submits it through `/isaac-feedback` on success. The CLI
validates the reason; the skill or native form owns submission.

Validate the form in Isaac, including the 16-character minimum, exact reason
submission, cancellation, repeated toggles and failures. These scenarios and a live
evidence upload are not covered by the automated suite.

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
