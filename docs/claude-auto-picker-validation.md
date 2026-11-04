# Claude Auto picker development validation

Date: 2026-11-04. Initial native validation: Claude Code 2.1.317; restored popup: 2.1.318.

This stack is a development draft, not ready for rollout. `/model` opens the
Auto/concrete mode popup, then Claude's native main-model picker. `/model auto`
opens the second picker directly. Confirm with Enter. The popup is the accepted
UX for this implementation; it does not inject a row into the native picker.

The native saved-choice journey verifies concrete selection, cancellation,
same-model Auto entry, subagent/orchestrator controls, the existing ON/routing-subagents
banner, popup cancellation and both popup choices, `DISABLED_AUTO`, and restoration
of the enabled recipe.
Auto keeps the existing smart-router footer. It does not add an Auto footer label.
These tests use the real binary with dummy credentials and no inference.

The native `s` session-only picker path bypasses `config.set`. Its regression test
is intentionally failing. Dispatch is held until an Enter-confirmed `/model` choice.
The native mod API has command and config hooks. An isolated Claude 2.1.318
organization-managed mod successfully returned a `settings.read` overlay, but the
native picker still showed the original rows and labels: its per-source settings
lookup bypasses that overlay. The probe changed no host managed settings.
`session.append` for native command completion is skipped by this machine's security
plugin; we do not change that policy.

## Evidence

- 1,133 focused Python config, CLI, smart-router, mod-generation and routing tests passed.
- Eight native mod/command tests passed, including concrete recipe tracking.
- The saved-choice TUI test passed; the session-only variant fails.
- Restoring the popup started with a failing native component test. On 2.1.318,
  the native TUI caught and then verified the callback fix: a mod calling
  `$.command.run` skips its own handler, so the popup must record its pending
  Auto/concrete choice before calling the native command. Seven native mod tests
  and the saved-choice popup TUI passed; 282 focused Python tests passed.
- On the final recipe branch, three selected native tests passed on 2.1.318:
  plugin validation/component tests, concrete recipe/body preservation, and the
  real popup TUI with enabled recipe restoration and `DISABLED_AUTO` on exit.
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

- Complete session-only selection, durable session resume, model/policy failure rollback,
  and cancellation at native dispatch after permission checks.
- Exercise required opt-out feedback, settings reload, compaction and concurrent spawns
  with live inference. Verify Auto on/off tracking in actual parent HTTP bodies.
- Validate rollout/build eligibility and explicit Auto-to-legacy rollback.

No live inference was rerun for the popup restoration. No model catalog or
managed-settings change was used to add an Auto model.
