# Codex routing-toggle restart

Smart-routed `ug codex` sessions restart their owned app-server after
`$smart-router off` or `$smart-router on`. The native TUI reconnects and resumes
the saved root; the parent reopens existing children with `followup_task`.
This is a temporary attribution workaround while native platform support is pending.
There is no gateway proxy or backend session state.

The Codex routing minimum remains 0.164.0 and the routing CI pin remains 0.165.0.
Installed-wheel restart/resume and attribution checks passed on Linux Codex
0.165.0. Codex 0.164.0 still needs dedicated restart validation.
The managed default remains Claude-only. Select a Codex-enabled preset:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1 ug codex
```

## Request path

```text
Codex TUI → UG control interposer → Codex app-server → AI Gateway
```

The interposer forwards native WebSocket control RPCs. Inference goes directly
from the configured Databricks provider to AI Gateway.

Initial launch and each restart set the provider's
`Databricks-Smart-Router-Recipe` header and
`responses_api_metadata.smart_router_recipe_name`. Codex serializes the latter
inside `client_metadata["x-codex-turn-metadata"]` in the request body.
Off sets both to `DISABLED`; on restores the launch recipe.

`ug configure` removes persisted recipe headers because OS-managed headers
outrank session configuration. An installation with an old managed recipe header
needs `ug configure --global` with `UG_SKIP_OS_SETTINGS` unset, or an administrator
to remove that header. User-only configure leaves machine-wide settings intact.
Unreadable or malformed managed settings report the affected path and repair steps.
On Windows,
`requirements.toml` replaces the provider block: attribution uses body metadata,
and the recipe header may be absent. It must never carry a stale value.

## Restart behavior

1. The skill atomically updates the existing session-control JSON. Routing hooks
   continue to read the same file. If the sandbox denies access to those private
   controls, the skill requests native command approval; without approval, routing stays unchanged.
   Temporary control-file read errors keep the last applied routing state; UG warns
   once and retries until the file is readable again.
2. UG stops its app-server, then starts a fresh process on the same WebSocket
   address with updated attribution. The TUI process and `CODEX_HOME` stay intact.
3. Native Codex reconnects and resumes the saved conversation. UG forwards that
   resume unchanged, leaving models, cwd, sandbox, and approvals to native Codex.
4. After a successful root resume, UG sends one recovery prompt only if native
   saved history or observed collaboration events show that root has children.
   With no known children, UG consumes the restart notice without starting a
   model turn; the next user message proceeds normally. The recovery prompt asks the
   parent to continue unfinished work and reopen existing children with
   `followup_task`, without spawning replacements or repeating the toggle.
   Unfinished children must be resumed even when the latest user prompt only
   changed Smart Router. Saved children can be absent from `list_agents` after
   restart; the parent recovers their task names and assignments from conversation history.

Temporary threads that Codex creates for conversation titles do not replace the
saved root ID used to match that resume.

Children retain their saved IDs and histories, but they are stopped until the
parent reopens them. UG does not enumerate descendants, reconstruct permission
settings, or run per-thread unload transactions. The first-prompt router does not
run again after a reconnect. Ordinary `/subagents` navigation causes no restart
or recovery prompt.

On POSIX, app-server shutdown signals its whole process group, including the npm
launcher and native server. After waiting up to five seconds for the launcher,
UG kills remaining descendants even if the launcher exited promptly.
Windows stops the whole app-server process tree with `taskkill`.
If a child exits during that traversal, UG still waits for its app-server handle
before deciding shutdown failed. A failed shutdown includes `taskkill` diagnostics.
This prevents a surviving native server from holding the replacement's port.
UG also waits for that port to stop accepting connections before starting the replacement.
Readiness gets 30 seconds. An unexpected server exit or failed
restart stops the owned TUI and reports recovery instructions. A rejected recovery
turn reports an error and asks the user to send a message to continue.
Restart continuations are logged in `~/.ucode/codex-v2-interposer.log`.
Native server diagnostics are retained in `~/.ucode/codex-app-server.log` and
included in integration-test artifacts.

## Recovery and limits

Saved history survives a restart failure. Start a new launch and use native resume:

```bash
ug codex -- resume ROOT_ID
```

Native subcommands bypass smart routing. The new launch uses its own routing and
permission settings; it does not restore the old session-control file.

Restart stops all running work in this launch's server, including loaded roots
other than the one currently displayed. Only the resumed root with known children
gets an automatic recovery prompt. Native history survives, but UG no longer promises automatic
continuation for multiple open roots, approval replies, queued input, or Escape
during a toggle. Native Codex owns reconnect and input handling.

Requests already sent keep their original attribution. A short interval between
the skill's file update and server shutdown can still carry the old value.
Running commands stop; completed effects and remote jobs are not rolled back.
The recovery prompt tells agents to inspect effects before retrying because
retries can duplicate effects. Assignment recovery depends on the parent
following those instructions.

## Validation

`tests/test_codex_routing_reload.py` covers server lifecycle, off/on configuration,
shutdown escalation, native descendant port release, Windows process-tree shutdown,
failed startup/restart, unexpected exit, reconnect without rerouting, root-only
continuation, no injected turn without children, child detection from native history
and streamed collaboration items, ephemeral title threads, coalesced notices, and private internal RPC
responses. Component checks simulate delayed Windows exit and failed termination;
malformed frames and null resume results do not close the relay or consume a pending
restart. Temporary control-file failures preserve the current session, and a stalled
TUI after Ctrl+C gets bounded cleanup.
A real subprocess-tree check requires the descendant's listening port to be released
on both Linux and Windows. Live Windows restart coverage comes from the installed-wheel journey.

Earlier Linux Codex 0.165.0 installed-wheel checks passed against the selected
`eng-ml-inference` profile before the command-completion receipt assertions were added:

- The routing-only skill journey toggles off and back on, completes real child
  calculations in each state, and exits normally.
- `test_smart_router_codex_reload_updates_parent_and_existing_child_inference`
  toggles off with two busy children and on with three. All original IDs and
  permissions survive; resumed children deliver their own final answers. Successful
  parent, existing-child, and new-child inference carries both the expected recipe
  header and body metadata after each restart. The real TUI exits normally.

The strengthened busy-child journey now requires exclusive command-completion receipts.
The latest local run completed both restarts and all five resumed child commands,
but failed the final new-child routing display check. The selected workspace's
router returned HTTP 422 for the offered models, also reproduced through a direct
API call without UG or the recorder. The full strengthened journey has not passed locally.

The busy-child check exposed and now guards against title threads replacing the
saved root, a native server surviving its npm launcher's forced shutdown, and a
recovery prompt letting the parent finish without resuming unfinished children.
The ordinary child-navigation journey passed Linux Codex 0.165.0 CI without a
restart or interrupted child command.

Separate Linux Codex 0.167.0 native checks confirmed saved child tool history and
root sandbox preservation. Earlier 0.164.0/0.165.0/0.167.0 live results for the
removed in-process controller are historical evidence only. Native macOS/Windows,
Codex 0.164.0, approval dialogs, compaction, and multiple open roots still need
dedicated restart validation.
