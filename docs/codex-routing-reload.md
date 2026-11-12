# Codex routing-toggle reload

Smart-routed `ug codex` sessions unload and resume their saved conversations after
`$smart-router off` or `$smart-router on`. The app-server process and TUI WebSocket
connection stay open. This avoids the reconnect screen caused by restarting the
app-server. Running turns and commands are still interrupted.

The Codex routing minimum remains 0.164.0 and the routing CI pin remains 0.165.0.
The managed default remains Claude-only. Select a Codex-enabled preset:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1 ug codex
```

## Request path

```text
Codex TUI → UG control interposer → Codex app-server → AI Gateway
```

The interposer handles native WebSocket control RPCs. Inference goes directly
from the configured Databricks provider to AI Gateway. There is no inference
proxy, backend session state, or native Codex change.

Codex builds its provider configuration when it loads a conversation. Resuming
an already-loaded conversation can ignore new overrides. UG therefore sets
`thread_unload_delay_secs=0`, unsubscribes the affected conversations, and verifies
through `thread/loaded/list` that they have actually unloaded before resuming.
`thread/resume` then loads the same saved root with updated configuration.

The provider's `Databricks-Smart-Router-Recipe` header and
`responses_api_metadata.smart_router_recipe_name` are updated together.
Codex serializes the latter inside
`client_metadata["x-codex-turn-metadata"]` in inference request bodies.
Off sets both to `DISABLED`; on restores the launch recipe. New roots and resumed
children inherit the current routing setting.

Opening an unfamiliar saved root applies current attribution on its initial resume.
UG first checks whether that conversation is already loaded; only an already-loaded
root needs an additional unload/resume. If that root and its children are idle and
routing has not changed, UG starts no continuation.

`ug configure` removes persisted recipe headers because OS-managed headers can
outrank session configuration. Old installations with pinned managed headers need
`ug configure --global` with `UG_SKIP_OS_SETTINGS` unset, or an administrator to
remove that header. User-only configure leaves machine-wide settings intact.
Unreadable or malformed managed settings report the affected path and repair steps.
On Windows, `requirements.toml` replaces the provider block: attribution uses body
metadata, and the recipe header may be absent. It must never carry a stale value.

## Reload behavior

1. Before a Codex toggle, the skill explains that the expected “Conversation
   interrupted” message comes from reloading routing settings and that the user can
   continue in the same conversation afterward. Repeating the current on/off state
   leaves controls untouched and starts no reload. A changed setting atomically
   updates the existing session-control file. Routing hooks
   read the same file. If sandbox access requires approval, the skill uses the
   native command-approval dialog. Temporary read errors retain the last applied
   setting and are logged once until controls become readable.
2. UG waits for accepted client requests to finish, then interrupts loaded roots
   and descendants. It discovers late children before unsubscribing and waits
   until the affected trees are unloaded. Unrelated conversations stay loaded.
3. UG resumes each affected root under its saved ID with the current attribution
   and its latest accepted model, working directory, sandbox, and approval settings.
   Automatic continuation uses those same settings, including the model nested in
   collaboration mode.
4. If actual work was interrupted, a continuation asks the parent to resume
   unfinished children with `followup_task`, without spawning replacements or repeating the toggle.
   Children can be absent from `list_agents` until reopened; the parent recovers
   task names from conversation history. This applies even when the latest user
   prompt only toggled Smart Router. An idle parent or a turn containing only
   `$smart-router on` or `$smart-router off`, with no interrupted children, resumes
   silently: UG sends no recovery prompt
   and starts no automatic turn. Completed idle children remain saved.

User input waits behind the reload; approval replies can still pass through.
Queued user input receives recovery context instead of a competing continuation,
even when it entered the queue before the reload began and is dequeued after it
finishes. Each queued request remains tracked until it is forwarded.
Escape cancels automatic continuation. Temporary title threads and ordinary
`/subagents` navigation do not replace the root or trigger a reload.
The first-prompt routing decision is retained across TUI reconnects.

The reload timeout is 30 seconds; individual RPCs have a 10-second timeout.
Failures, including malformed RPC responses, report the saved root ID and block
held input on that connection.
Diagnostics are retained in `~/.ucode/codex-v2-interposer.log` and
`~/.ucode/codex-app-server.log`. Unsupported sandbox profiles leave ordinary
sessions running; a toggle fails before interruption if UG cannot preserve that
profile. A partial reload does not release queued input against uncertain attribution.

## Recovery and limits

Saved history survives a reload failure. Start a new launch and use native resume:

```bash
ug codex -- resume ROOT_ID
```

Native subcommands bypass smart routing. The new launch uses its own routing and
permission settings; it does not restore the old session-control file.

Requests already sent keep their original attribution. The interval between the
skill's file update and conversation interruption can still use the old recipe.
Running commands stop; completed effects and remote jobs are not rolled back.
Recovery asks agents to inspect completed effects and rerun commands that did not
exit successfully. Retries can duplicate effects; recovery depends on agents
following those instructions. Keeping the connection open removes restart-driven
reconnection; normal UI redraws during interruption and resumption can still occur.

## Validation

Component coverage includes off/on restoration, late descendants, unrelated roots,
per-root permissions, queued input, approval-reply draining, Escape, timeout/failure
reporting, rapid toggles, root navigation, malformed frames, and transient control
errors. Launcher checks require one app-server across toggles and retain bounded
POSIX/Windows process-tree cleanup on final exit.

The installed-wheel busy-child journey requires unchanged app-server PIDs and one
TUI connection, saved child IDs, preserved sandbox/approval settings, exclusive
command-completion receipts, child-owned final answers, and successful parent and
child requests with current attribution. Separate journeys cover ordinary navigation
and installed-skill off/on toggles. Idle-toggle journeys require exactly the skill
turn and an explicit user probe, with no automatic turn, both with no children and
with a completed idle child. A recording test fixture forwards real gateway
traffic unchanged; it is not part of the production request path.

On 2026-11-12, 796 focused component/contract tests and the lint gate passed
after the follow-up review and idle-toggle fixes. Socket-collision checks also passed with an
unrelated listener on the mocked launcher port. The installed-wheel results below
are from revision `2bde6427`, before those follow-up fixes, in a fresh Linux Arclet
against the explicitly selected `eng-ml-inference` profile with router `m2-r315-quality-20260929`:

- Codex 0.167.0 passed all four selected journeys: installed-skill off/on with
  `subagent_orch_v0`, ordinary running-child navigation, no-child attribution, and
  busy-child recovery with `subagent_orch_v1`.
- Parent and child requests used the selected recipe, then `DISABLED`, then the
  selected recipe again in both header and body metadata. App-server PIDs and
  root/child IDs stayed unchanged, and the TUI used one connection per session.
- Busy-child recovery required exclusive command-completion receipts and
  child-owned final answers from two original children after off and three after
  on. New children routed successfully after on. Sandbox and approval settings
  were preserved, and each TUI exited normally.
- No-child reloads took 0.179/0.183 seconds; busy-child reloads took 0.298/0.298
  seconds, excluding model completion. Native toggle approvals preserved the
  read-only sandbox in the no-child journey.

Earlier runs with the workspace's default `stable` recipe could not establish
successful model routing: direct routing requests returned HTTP 422. Supplying
`m2-r315-quality-20260929` resolved that failure without relaxing assertions.
The runner now accepts and records `--smart-router-name`, and passes only that
selected recipe into each isolated agent environment.

Codex 0.165.0 also passed installed-skill off/on with `subagent_orch_v0`, no-child
attribution, and the complete busy-child journey with the same recipe. Seven
selected live journeys passed across the two versions. Historical results are
not evidence for this revision. Native Windows, macOS, Codex 0.164.0, a reload
while an approval is already pending, compaction, and multiple-root live recovery
need separate validation.
