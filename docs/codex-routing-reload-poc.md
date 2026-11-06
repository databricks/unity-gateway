# Codex routing-toggle reload POC

This opt-in launcher makes `$smart-router off` update inference attribution for
the existing Codex conversation and its native children. It interrupts and unloads
the conversation tree, then resumes the same saved root with the new recipe.
`$smart-router on` repeats that process with the original launch recipe.

The normal `ug codex` launch path is unchanged. This is a POC, tested on Linux with
Codex **0.166.0**, not a supported rollout or a compatibility claim for other versions.

## Run

Use a configured Codex installation with native multi-agent v2 model metadata and
access to the selected workspace. From this checkout, with its dependencies available:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1 \
PYTHONPATH=src python -m ucode.smart_routing.codex_reload_poc \
  --workspace https://eng-ml-inference.staging.cloud.databricks.com \
  --profile eng-ml-inference \
  --model system.ai.gpt-6-1-sol \
  -- --no-alt-screen
```

Use the actual Smart Router skill in the TUI. The launcher reuses UG's installed
skills, routing hooks, authentication, provider configuration, and existing
session-control JSON file. It does not change the globally selected routing preset.
The explicit preset matters: UG's default enabled preset is currently Claude-only.

## How it works

```text
Codex TUI → UG app-server WebSocket interposer → Codex app-server
                                                   ↓
                                              AI Gateway
```

The interposer carries native control RPCs. It does not forward or rewrite
inference HTTP requests; the configured Databricks provider sends those directly
to AI Gateway. There is no gateway proxy or backend session-state service.

1. The skill atomically updates the existing session-control file. Hooks read that
   file to decide whether routing and orchestration are enabled.
2. The controller notices the change, holds new TUI requests, and discovers loaded
   descendants using `thread/loaded/list` and `thread/read`.
3. It sends `turn/interrupt`, waits for active turns to end, and sends
   `thread/unsubscribe` for children and root. Repeated discovery catches children
   created during interruption. Unrelated roots are left alone.
4. With `thread_unload_delay_secs=0`, it verifies the tree is unloaded, then calls
   `thread/resume` for the original root ID with updated provider configuration.
   Existing children reload from their saved sessions when resumed by the parent.
5. A continuation turn tells the parent which children were interrupted and asks
   it to resume those assignments through `followup_task`. It warns that commands
   may already have produced effects and must be inspected before retrying.

The resumed configuration sets both:

- Provider header `Databricks-Smart-Router-Recipe`.
- `responses_api_metadata.smart_router_recipe_name`, serialized by this Codex
  version inside `client_metadata["x-codex-turn-metadata"]` in the request body.

Off uses `DISABLED`; on restores the launch recipe, which was `stable` in the live
checks. Before the first reload, the ordinary UG overlay supplies the header; this
POC adds the body field on reload. It preserves captured model, approval, sandbox,
and working-directory settings. Ephemeral TUI title/helper threads cannot replace
the tracked root or its permissions.

## Recovery

The controller logs transitions to `~/.ucode/codex-v2-interposer.log`. A failed
reload closes the control connection and reports the saved root ID and controls
path. Reopen using the same checkout, Codex home, workspace, model, and preset:

```bash
SMART_ROUTER_CONFIG_VERSION=subagent_orch_v1 \
PYTHONPATH=src python -m ucode.smart_routing.codex_reload_poc \
  --workspace https://eng-ml-inference.staging.cloud.databricks.com \
  --profile eng-ml-inference \
  --model system.ai.gpt-6-1-sol \
  --session-controls /path/from/reload-log/env.json \
  -- resume ROOT_ID
```

Do not forward `-a` or `-s` on this remote `resume` command: Codex 0.166.0 rejects
permission overrides on remote resume. In the live check, full process restart
restored conversation history and routing state but reset the sandbox from
workspace-write to read-only. Restore the intended permission mode in the native
TUI before continuing file-changing work. Automatic in-session reload preserves
the existing permissions; this limitation applies to restarting the process.

Reusing the controls file preserves off/on state. Without that argument, a new
launch gets fresh controls and the selected launch preset. The controls file is
currently temporary; recovery across machine reboots is not covered.

## Validation

Manual real-TUI checks used the explicitly selected `eng-ml-inference` workspace,
Codex 0.166.0, `system.ai.gpt-6-1-sol` for the parent, and native `gpt-6-luna`
children. An isolated user-owned model catalog included native aliases and
service IDs. Hook trust was accepted through the actual TUI. Mitmproxy was used
only as an observation tool for validation; it is not part of this implementation.
The recorder retained allowlisted thread IDs, models, recipe fields, timestamps,
and HTTP status codes, excluding prompts and credentials.

- Actual `$smart-router off` → `$smart-router on` completed in the same root.
- A child running a local command was interrupted, then automatically resumed as
  the same child. Parent and child requests used `DISABLED` while off, then the
  launch recipe after on. A newly spawned child after on also used that recipe.
- Persisted turn contexts kept the same working directory, approval policy, and
  workspace-write sandbox settings. Both sessions exited normally.
- A separate process reopened the saved root while off, continued the original
  child, and spawned a new child. All captured recovery requests carried
  `DISABLED` in both fields, and each child persisted its own completed reply.
  Recovery exited normally, with the sandbox reset described above.
- The controlled command wrote its start marker twice after interruption and
  retry. This demonstrates repeated effects, not exactly-once command execution.

Component coverage in `tests/test_codex_routing_reload.py` exercises late
descendants, unrelated-root isolation, off/on restoration, permission preservation,
failure reporting, held requests, draining later approval replies, and ephemeral
TUI helpers. These controlled transport checks do not establish native approval
dialog behavior. The manual checks are separate from the installed-wheel
integration suite and are not claimed as a CI integration pass.

Local verification passed 424 selected unit/component tests across the reload,
Codex launch, configuration, skill, and integration-contract suites. The full
repository lint gate (Ruff lint/format on `src/` and `tests/`, plus package-wide
type checks) passed. The separate extended Ruff command also checked
`scripts/run_integration.py` and found two existing direct-subprocess violations
there; that file is unchanged. No full live CI suite was run.

## Limits before rollout

- Requests already sent cannot change attribution. The new value is guaranteed
  only for requests from the resumed conversation after reload completes; there
  can be a short old-value interval after the skill writes the controls file.
- Child resumption depends on the parent following the continuation instruction.
  The demonstrated case succeeds; arbitrary task recovery is not guaranteed.
- Only the active saved root and its native descendants are targeted. Multiple
  simultaneous root conversations, TUI helper inference, `/new`, compaction,
  reconnects during reload, rapid repeated toggles, and native approval dialogs
  need dedicated live coverage.
- Remote jobs and already completed effects are not rolled back. Local commands
  may have to restart, and retry can duplicate effects.
- Full process recovery requires restoring the permission mode in the TUI;
  persisting and restoring those settings automatically is deferred.
- Native Windows/macOS, subscription relay, and other Codex versions are untested.
  The launcher rejects subscription relay.
- First-prompt routing retains the existing interposer path but has not been
  live-tested with this POC; the successful runs used `subagent_orch_v1`.
