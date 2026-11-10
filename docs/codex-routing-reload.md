# Codex routing-toggle restart

Smart-routed `ug codex` sessions restart their owned app-server after
`$smart-router off` or `$smart-router on`. The native TUI reconnects and resumes
the saved root; the parent reopens existing children with `followup_task`.
This is a temporary attribution workaround while native platform support is pending.
There is no gateway proxy or backend session state.

The Codex routing minimum remains 0.164.0 and the routing CI pin remains 0.165.0.
The restart/resume checks described below used Linux Codex 0.167.0; those checks
have not established the new implementation's behavior on the older versions.
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
needs reconfiguration through the existing managed-write flow. On Windows,
`requirements.toml` replaces the provider block: attribution uses body metadata,
and the recipe header may be absent. It must never carry a stale value.

## Restart behavior

1. The skill atomically updates the existing session-control JSON. Routing hooks
   continue to read the same file.
2. UG stops its app-server, then starts a fresh process on the same WebSocket
   address with updated attribution. The TUI process and `CODEX_HOME` stay intact.
3. Native Codex reconnects and resumes the saved conversation. UG forwards that
   resume unchanged, leaving models, cwd, sandbox, and approvals to native Codex.
4. After a successful root resume, UG sends one recovery prompt. It asks the
   parent to continue unfinished work and reopen existing children with
   `followup_task`, without spawning replacements or repeating the toggle.

Children retain their saved IDs and histories, but they are stopped until the
parent reopens them. UG does not enumerate descendants, reconstruct permission
settings, or run per-thread unload transactions. The first-prompt router does not
run again after a reconnect. Ordinary `/subagents` navigation causes no restart
or recovery prompt.

Each process shutdown gets five seconds before a forced kill, followed by a
bounded wait. Readiness gets 30 seconds. An unexpected server exit or failed
restart stops the owned TUI and reports recovery instructions. A rejected recovery
turn reports an error and asks the user to send a message to continue.
Restart continuations are logged in `~/.ucode/codex-v2-interposer.log`.

## Recovery and limits

Saved history survives a restart failure. Start a new launch and use native resume:

```bash
ug codex -- resume ROOT_ID
```

Native subcommands bypass smart routing. The new launch uses its own routing and
permission settings; it does not restore the old session-control file.

Restart stops all running work in this launch's server, including loaded roots
other than the one currently displayed. Only the resumed root gets an automatic
recovery prompt. Native history survives, but UG no longer promises automatic
continuation for multiple open roots, approval replies, queued input, or Escape
during a toggle. Native Codex owns reconnect and input handling.

Requests already sent keep their original attribution. A short interval between
the skill's file update and server shutdown can still carry the old value.
Running commands stop; completed effects and remote jobs are not rolled back.
The recovery prompt tells agents to inspect effects before retrying because
retries can duplicate effects. Assignment recovery depends on the parent
following those instructions.

## Validation

`tests/test_codex_routing_reload.py` replaces tests of the removed in-process
controller with server lifecycle, off/on configuration, shutdown escalation,
failed startup/restart, unexpected exit, reconnect without a second routing
selection, root-only continuation, coalesced notices, and private internal RPC
response coverage. These component tests do not establish live gateway behavior.

Linux Codex 0.167.0 native checks confirmed that restarting app-server preserves
the root sandbox and saved child history. Children remained stopped after root
resume; `followup_task` reopened the same child ID, recalled completed tool
output after its source file was deleted, and completed interrupted work.
A real TUI also survived restart and completed inference before and after it
under the same root history. A passive control-protocol check confirmed automatic
`thread/resume` on reconnect with no stale config overrides. These were native
recovery checks, not an
installed-wheel off/on attribution pass.

Live attribution on the selected `eng-ml-inference` profile remains unverified:
this host's managed Codex configuration pins another provider endpoint and
outranks launch overrides. The native recovery checks used that host-managed
provider, so they do not prove the selected workspace or recipe changed.

The retained installed-wheel journey
`test_smart_router_codex_reload_updates_parent_and_existing_child_inference`
invokes the skill with busy children, checks stable IDs and permissions,
child-owned final answers, and both recipe fields on successful inference from
parents, existing children, and new children after off and on. The navigation
journey opens a busy child and returns to the parent without interrupting work.
Neither journey has passed for this restart implementation yet.

Earlier Linux live results on 0.164.0, 0.165.0, and 0.167.0 validated the removed
in-process controller. They are historical evidence only. Native macOS/Windows,
older supported versions, approval dialogs, compaction, and multiple open roots
need dedicated restart validation.
