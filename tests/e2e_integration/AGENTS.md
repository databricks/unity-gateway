# Full E2E CUJ instructions

These instructions are shared by Codex and Claude Code. `CLAUDE.md` imports this
file; keep all suite guidance here.

This suite contains full journeys against real workspaces and installed agents,
without stubbed configuration. The existing `tests/integration/` suite remains
separate, including its `managed_fixture` cases. No existing tests have moved.

## Primary requirement: one Databricks workspace per test

Every test invocation must have its own exclusively assigned Databricks workspace.
This means a distinct workspace identity, not just a different local directory,
schema, config name, or session. Claude and Codex tests get separate workspaces;
parallel workers, developers, and CI runs must not share an assigned workspace.

Use function-scoped allocation and hold ownership through setup, all phases of
the journey, and verified cleanup. Each test sets up its required real configuration
in its own workspace and must run independently, without relying on another test's
state or execution order. A pooled workspace may be reassigned only after cleanup
is verified; quarantine it on cleanup failure and report recovery information.

A lock serializing tests against one shared workspace does not meet this requirement.
Do not fall back to a shared workspace when allocation is unavailable: fail before
configuration or inference. Record each test's workspace identity in its artifacts,
without credentials. Implement allocation before adding executable journeys.

The current suite is scaffolding only: `test_cuj_smart_routing.py` specifies future
journeys and collects no tests. Do not add passing or skipped placeholders, or
convert empty collection into a successful E2E run. Process, terminal, evidence,
workspace-lifecycle, and agent-selection helpers are not implemented yet.

## Adding journeys

- Name every journey module and function `test_cuj_*`. Keep tests at this directory's
  root, with separate explicit Claude and Codex functions and `Scenario:` / `Expected:`
  docstrings. Keep configure, launch, task, and assertions visible in each test.
- Put independently written reusable functionality in `helpers/`. Fixtures supply
  isolated environments and credentials, not preconfigured application state.
  Add helpers alongside the journeys that need them, not unused implementations.
- Mark journeys `live`, interactive journeys `tui`, and each agent's tests `claude`
  or `codex`.
- Exercise installed public CLIs and real services. Do not import `ucode` internals,
  existing `test_e2e*` modules, or `tests/integration/utils` helpers.
- No mocks, monkeypatching, config stubs, fake services, replacement executables,
  or fabricated ug state. The parent suite's `managed_fixture` exception does not
  apply here, including `UCODE_MANAGED_CONFIG_STUB`.
- Require observable task completion and session-correlated evidence. A startup,
  banner, echoed prompt, or tool output alone is insufficient. Interactive coverage
  must drive the real TUI and first-prompt path.
- Use unique homes, projects, prompts, and artifacts; bounded process lifetimes;
  and cleanup on failure. Isolate machine-wide settings on a disposable runner.
  Verify workspace ownership before mutation and cleanup; never overwrite
  unexpected admin edits or release an allocation with unverified cleanup.
- Poll observable conditions with deadlines; retry allocation waits and publication
  visibility only. Exclude stale records with per-launch evidence boundaries.
  Smart-routing assertions must not require a particular winning model. State
  the limits of native model evidence rather than claiming server-side verification.
- Do not hide failures with skips, xfails, task retries, or weaker assertions.
  Do not add production behavior solely for tests. Keep secrets out of artifacts.

## Validation and reporting

Run these commands from the repository root. Inspect collection without installing
agents or contacting a workspace:

```bash
uv run pytest -c tests/e2e_integration/pytest.ini \
  --confcutdir=tests/e2e_integration tests/e2e_integration --collect-only
```

Expect pytest exit code 5 until executable journeys are added. Always use the
suite's own pytest configuration and fixture boundary; never inherit the unit
suite's autouse mocks. Ordinary `uv run pytest` excludes this directory.

Once allocation and journeys are implemented, run them through
`scripts/run_integration.py --suite e2e-integration` on a clean disposable POSIX
runner with exact versions and explicit authentication. Never select a developer
profile automatically. The runner's current single `--workspace` input is not a
per-test allocator and must not be used to share a workspace across this suite.
Wire per-test allocation and matching credentials before enabling live execution.
`--installation-only` and `--headless-only` belong to the existing `integration` suite.

The runner records the selected suite, versions, dependencies, JUnit results,
and artifacts. Missing prerequisites and empty selections fail honestly. Add no
live CI lane until there is an executable journey.

Keep local helper/contract tests in the ordinary unit suite. Current checks live
in `tests/test_cuj_e2e_contract.py` and `tests/test_integration_runner.py`. Update
this file and coverage documentation when implemented coverage changes. Report
local checks, live results, and unexecuted scenarios separately.
