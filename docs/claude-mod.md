# Claude mod entry point

UG's Claude launcher loads `hooks/register.ts` through the existing launch-scoped
`ug-smart-router` plugin in sessions launched with smart routing. Claude Code 2.1.290 or
newer is required.

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
matching Agent row shows a spinner and elapsed seconds while the router answers. Claude
hides that row once a second Agent call joins it, so while any call is routing, the turn's
spinner line also reads `Selecting a model… Ns` or `Selecting models for N agents… Ns`,
timed from the oldest waiting call; Claude's own glyph and counters stay.
A lone call's decision is drawn under its Agent row: `↳ Selected Model:` in green with its
routing time, then the indented `Reason:`. Claude folds parallel Agent rows into its own
"Running N agents" block, which raises no render hook, so calls that were routing at the same
time are drawn instead as cards (bold task, selected model, rationale) above the parent's
next reply, in call order. A reply is claimed by its message id on its first block drawn after
routing; the message that issued the calls is drawn before routing finishes and is never used,
a call still routing waits for a later reply, and a call whose routing failed draws no card.
Cards survive a turn that ends while the reply waits on a background agent and are dropped
only when the turn is aborted. Nothing is drawn above the prompt and nothing is logged.
Decisions are kept for the session; a plugin reload clears them from the screen, and the
routing decision audit log is unchanged.
Disabled routing, command failures, and invalid responses keep the original call;
failures also log a diagnostic.

In eligible Smart Router Orchestrator launches, `tool.describe` replaces the native
Agent/Task delegation advice with the orchestrator's instructions. It changes only the
description, preserving the native input schema and execution. The host's `Usage notes`
section stays intact for native continuation mechanics. The orchestrator instructions
explicitly tell the parent to end its current turn when only background work remains
and wait for the completion notification, without launching another worker to wait.
Other native ownership and task-selection advice is replaced. Routing toggles invalidate
Claude's description cache: off restores native advice, and on restores the launch's
orchestrator advice. Routing-only launches retain native advice. The same concise
instructions lead the Claude activation context so its delegation rule survives a
truncated preview of the full workflow.

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

Before each on-to-off change, Isaac sessions with
`ISAAC_SMART_ROUTER_FEEDBACK=1` or `true` require a reason of at least 16 characters after
trimming surrounding whitespace. Isaac sets this flag from its
rollout setting or the user's explicit `1`/`0` or `true`/`false` override. As with UG's other
boolean environment flags, `1`, `true`, `yes`, and `on` enable it (case-insensitive,
with surrounding whitespace ignored); other values disable it. It controls Isaac feedback
availability, so UG's routing presets and on/off toggles leave it unchanged.
The native form keeps routing on until Enter accepts a valid reason. It closes the
form and shows the sending message and feedback ID before starting Isaac. The existing
native background callback runs `isaac feedback --with-evidence` to capture and upload
the report while the user continues working. Routing is turned off independently of
that process. Delivery produces both a persistent message and a success toast;
a failed upload offers `/isaac-feedback retry <id>` when a local snapshot was saved.
Before submitting, Cancel or Escape leaves routing on and sends nothing. A missing
`isaac` executable at launch or an unavailable form leaves routing on. A feedback submission failure after a valid
reason leaves routing off and reports the failure in the transcript. The form makes no model requests.

In these sessions, with the same flag enabled, `/isaac-feedback` uses the same native
form, capture, and background uploader as `/smart-router off`. It remains available
after routing is turned off so users can report on an earlier routed task. It asks
for a category when none was supplied, and for feedback text when the invocation is empty.
`/isaac-feedback --category smart-routing <text>` submits directly. Smart-routing
reports pin the current session's most recent task by its native user-message ID.
The `session.append` hook remembers that ID; a session with no observed prompt uses
Isaac's existing target inspection before opening the form. Empty sessions use an
unmatched `no-prior-task` selector so subsequent work cannot become their target.
The native flow does not generate an agent note. Other reports retain their text-only
transport and do not receive an evidence feedback ID. `/isaac-feedback retry <id>` uploads the saved
snapshot in the background without capturing it again.

The submission and its sending/completion messages make no model requests and
need no updated Isaac skill instructions. Explicit inspection and local-preview
requests still use the existing skill. Earlier targets, agent notes, and additional
files remain available through the existing Isaac CLI options.

Claude sessions launched without smart routing use the existing feedback skill.

Each submission passes Claude's native session ID and the current routing controls
to Isaac. Valid session-file overrides are copied into that child process's environment,
so turning routing off or back on cannot change the report's recorded settings.
These settings describe submission time; subagent execution still requires routing-log
evidence. An invalid controls file remains unverified.

These uploads belong to the running Claude session. Keep it open until completion;
if interrupted, retry the saved evidence ID on the same machine. If capture did not
finish, submit the feedback again. No Isaac background worker or new CLI options are
required. Evidence is collected in the background: the selected task stays fixed,
while transcript and file snapshots reflect capture time and available file history.

`isaac-feedback.ts` owns this Databricks-specific policy, form, and submission.
`register.ts` binds the routing callbacks before Isaac's command guard, then
handles toggles the guard passes through. Claude's mod compiler requires engine
API calls to stay in the file that receives `$`.
`routing-state.ts` supplies the shared enabled check; launch options, live
environment values, and resolved session overrides use the same check on their
respective values.

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
It also checks Agent/Task descriptions in those requests: orchestrator instructions during
active phases, native guidance otherwise, without the conflicting native advice to avoid
spawning when unsure.

The `Claude mod · 2.1.290` CI job validates the packaged module and checks event
forwarding and launch-local options. Native request-construction checks cover description
changes across on/off/on, retained lifecycle guidance, an unchanged Agent schema, and
ineligible launches. They inspect
Claude's recorded request shapes after deliberately unauthenticated requests; they do not
establish successful inference or delegation quality. To run those component checks locally:

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
