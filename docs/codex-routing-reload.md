# Codex routing-toggle reload

Smart-routed `ug codex` sessions reload saved conversations after `$smart-router
off` or `$smart-router on`. This refreshes inference attribution for the parent,
existing children, and new children without a gateway proxy or backend session state.
It retains the existing Codex 0.164.0 minimum and 0.165.0 routing CI pin.
The managed default remains Claude-only; select a Codex-enabled preset to use this path:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1 ug codex
```

Use the installed Smart Router skill in the TUI. Ordinary non-routed launches
retain their existing behavior.

## Request path

```text
Codex TUI → UG control interposer → Codex app-server → AI Gateway
```

The interposer handles native WebSocket control RPCs. Inference HTTP requests go
directly from the configured Databricks provider to AI Gateway.

The initial launch and every reload set both:

- Provider header `Databricks-Smart-Router-Recipe`.
- `responses_api_metadata.smart_router_recipe_name`. Codex serializes this
  inside `client_metadata["x-codex-turn-metadata"]` in the request body.

Off sets both to `DISABLED`; on restores the launch recipe.

`ug configure` removes recipe headers from persisted provider settings. OS-managed
headers outrank per-session configuration and would otherwise pin the old value.
On Windows, `requirements.toml` replaces the provider block: attribution uses the
body metadata, and the recipe header may be absent. It must never carry a stale
value. Upgrading an installation with an old managed recipe header requires
reconfiguration so it can be removed through the existing managed-write flow.

## Reload transaction

1. The skill atomically updates UG's existing session-control JSON file. Hooks
   read that same file when deciding whether to route or inject orchestration.
2. The controller holds new user requests and waits for already submitted
   settings changes to finish. Approval replies can still pass through.
3. It discovers loaded descendants, interrupts active turns, and unsubscribes
   children and roots. `thread_unload_delay_secs=0` allows immediate unload.
   Repeated discovery catches children spawned during interruption.
4. After confirming unload, it resumes each affected root under its original ID
   with updated attribution and its confirmed model, cwd, and permission settings.
   Roots opened through this TUI are tracked separately; unrelated roots are left alone.
5. A continuation asks the parent to resume interrupted children with
   `followup_task`. Children reopen their saved conversations under the updated
   parent configuration. No replacement agents are requested.

A toggle arriving during resume causes another unload/resume before continuation.
Escape during reload suppresses automatic continuation. A queued new user turn
receives the recovery context instead of competing with an automatic turn.
New roots receive current attribution. Resuming a previously untracked root
forces a reload because native resume can otherwise ignore changed
configuration. Selecting a child in `/subagents`, or returning to an already tracked
root, leaves the tree running. Child navigation never changes which thread owns
the tree or overwrites the root's settings.

The transaction has a 30-second deadline and 10-second per-RPC timeout. Failure
blocks pending input, reports the saved conversation ID, and closes the control
connection. Transitions are logged in `~/.ucode/codex-v2-interposer.log`.

## Recovery and limits

Saved conversation history survives a failed reload. Exit and recover with the
normal native resume command:

```bash
ug codex -- resume ROOT_ID
```

This is conversation recovery, not a continuation of the old launch's routing
controls. Native subcommands bypass smart routing; check the new launch's routing
and permission settings before continuing. Full-process recovery does not restore
the old controls file automatically.

Requests already sent keep their original attribution. The new recipe applies
to inference from the resumed conversation; a short interval between the skill's
file update and interruption can still carry the old value.

Running commands stop. Completed effects and remote jobs are not rolled back;
retries can duplicate effects. The continuation explicitly tells agents to inspect
effects before retrying. Resuming arbitrary assignments still depends on the
parent following that instruction.

## Tests

`tests/test_codex_routing_reload.py` covers late descendants, root isolation,
off/on restoration, permission preservation, rejected settings changes, timeout
and resume failures, held input, approval replies, rapid toggles, Escape, and
reconnects with controlled external transports. Navigation regressions cover
children and nested descendants, both parent metadata shapes, partial metadata,
returning to a known root, and a subsequent real toggle.

`test_smart_router_codex_agent_navigation_preserves_running_child` opens a busy
child through the real `/subagents` picker and returns to the parent without invoking
the routing skill. It requires the command to finish exactly once under the same
child ID, no interruptions or generated reload prompts, and a working parent turn.
On Linux with Codex 0.167.0, this failed before the navigation fix: selecting the
child triggered a reload and a native error rejecting direct input to a subagent.
With the fix, the same journey passed with no reload and no restarted command.

The installed-wheel journey
`test_smart_router_codex_reload_updates_parent_and_existing_child_inference`
runs real `$smart-router off/on` from the main agent, with two children running
when switching off and all three existing children running when switching on.
It checks the actual header and body recipe on every resumed parent and child
request, including a new child spawned after each toggle. It also requires successful gateway responses, child-owned answers under stable IDs,
preserved sandbox settings, and normal TUI exit. A test-only recorder forwards
requests unchanged; it is not part of the product.

Select it through `scripts/run_integration.py` with an explicit workspace,
credentials/profile, `--ug-version checkout --codex-version 0.165.0`, and
`-- -k test_smart_router_codex_reload_updates_parent_and_existing_child_inference`.

The expanded journey passed on Linux with both supported versions against
`eng-ml-inference` on 2026-11-06. Independent mitmproxy captures confirmed:

- Codex 0.164.0: 22 requests after off and 18 after on.
- Codex 0.165.0: 19 requests after off and 13 after on.

Every request carried `DISABLED` or the launch recipe (`stable`), respectively,
in both header and body and received HTTP 200. Each run covered the parent and
three children after off, then the parent and all four children after on.
Original IDs, permissions, child-owned final answers, and normal TUI exit were
verified. These results do not require increasing the existing version floor.

After the navigation fix, both the navigation journey and the expanded busy-child
off/on journey passed on Linux with Codex 0.167.0. The test recorder checked 19
requests after off across four threads and 22 after on across five threads. All
had matching header/body recipes and successful gateway responses. The reload
transactions took 0.133s and 0.180s, excluding subsequent model work. This run
used the test recorder; independent mitmproxy validation above covers 0.164/0.165.

Mitmproxy was used only for this validation, scoped to the selected workspace
with process-local certificate trust. It captured allowlisted recipe, model,
thread, and response-status fields; no credentials or prompts. The test's
recorder forwarded unchanged requests through mitmproxy to the real gateway.
Neither recorder nor mitmproxy is used by product inference.

Earlier Linux validation on 0.166.0 passed the original one-busy-child journey
and the existing `subagent_only_v1` off/on journey. The original busy-child test
failed before the managed-header fix: body metadata changed but the pinned
header stayed `stable`.

Native approval dialogs during reload, compaction, simultaneous roots, and
Windows/macOS reload behavior still need dedicated live validation. Component
tests do not establish those live guarantees. First-prompt routing retains its
existing decision path; the busy-child journey uses `subagent_orch_v1`.
