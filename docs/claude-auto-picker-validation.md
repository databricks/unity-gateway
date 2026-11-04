# Claude Auto picker development validation

Date: 2026-11-04. Native CLI: Claude Code 2.1.317.

This stack is a development draft, not ready for rollout. The requested Auto row
inside the native `/model` picker is still missing. `/model` opens the native picker;
`/model auto` opens it with a pending Auto choice. Confirm with Enter.

The native saved-choice journey verifies concrete selection, cancellation,
same-model Auto entry, subagent/orchestrator controls, the existing ON/routing-subagents
banner, direct native `/model`, `DISABLED_AUTO`, and restoration of the enabled recipe.
Auto keeps the existing smart-router footer. It does not add an Auto footer label.
These tests use the real binary with dummy credentials and no inference.

The native `s` session-only picker path bypasses `config.set`. Its regression test
is intentionally failing. Dispatch is held until an Enter-confirmed `/model` choice.
The native mod API has command and config hooks, but no verified hook for adding an
action row to the resolved native model picker. `session.append` for native command
completion is skipped by this machine's security plugin; we do not change that policy.

## Evidence

- 1,133 focused Python config, CLI, smart-router, mod-generation and routing tests passed.
- Eight native mod/command tests passed, including concrete recipe tracking.
- The saved-choice TUI test passed; the session-only variant fails.
- Ruff check, Ruff format check and ty passed.
- Live UG launch against `https://dbc-a5d4177a-49dc.cloud.databricks.com`, using its
  saved `devtools-prod` profile, returned `AUTO_MAIN_OK`.
- A second live launch used one routed native child and returned `AUTO_CHILD_OK 4`.
  The root stayed on `system.ai.claude-opus-5-5`; the native child transcript recorded
  `system.ai.claude-sonnet-5-5`, matching its generated exact-model agent definition.

The live launches used the new preset and this checkout via `uv run --frozen ug claude
--skip-preflight --workspace ... -- --print ...`. They used existing local managed
configuration. They are live smoke checks, not isolated integration-suite results.

## Remaining acceptance work

- Add Auto as an action inside the native picker, without a wrapper menu or changing
  managed model settings. This needs a native extension not found in the installed build.
- Complete session-only selection, durable session resume, model/policy failure rollback,
  and cancellation at native dispatch after permission checks.
- Exercise required opt-out feedback, settings reload, compaction and concurrent spawns
  with live inference. Verify Auto on/off tracking in actual parent HTTP bodies.
- Validate rollout/build eligibility and explicit Auto-to-legacy rollback.

No model catalog or managed-settings change was used to add a fake Auto model.
