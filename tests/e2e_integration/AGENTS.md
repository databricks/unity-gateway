# Full E2E CUJ instructions

These instructions are shared by Codex and Claude Code. `CLAUDE.md` imports this
file; keep all suite guidance here.

This suite contains full journeys against real workspaces and installed agents,
without stubbed configuration. The existing `tests/integration/` suite remains
separate, including its `managed_fixture` cases. No existing tests have moved.

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
  Coordinate workspace-wide mutations across machines and verify restoration;
  never overwrite unexpected admin edits or steal a live lock.
- For singleton workspace policy, hold one cross-machine lock through setup,
  tasks, and verified restoration. Retain the lock and recovery guidance if
  restoration fails. Unrelated clients do not honor the lock, so use a workspace
  reserved for publication tests.
- Poll observable conditions with deadlines; retry lock contention and publication
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

Once journeys are implemented, run them through the existing installer:

```bash
python scripts/run_integration.py --suite e2e-integration \
  --ug-version checkout --claude-version 2.1.280 --codex-version 0.154.0 \
  --workspace https://dbc-1a9622fc-2e91.cloud.databricks.com \
  --profile YOUR_PROFILE -- -k test_cuj_smart_routing
```

Use a clean disposable POSIX runner, exact versions, and explicit authentication
via a selected profile, `DATABRICKS_BEARER`, or the runner's supported service-principal
credentials. Never select a developer profile automatically. The workspace above
is the planned fixture, not an implicit default. `--installation-only` and
`--headless-only` belong to the existing `integration` suite.

The runner records the selected suite, versions, dependencies, JUnit results,
and artifacts. Missing prerequisites and empty selections fail honestly. Add no
live CI lane until there is an executable journey.

Keep local helper/contract tests in the ordinary unit suite. Current checks live
in `tests/test_cuj_e2e_contract.py` and `tests/test_integration_runner.py`. Update
this file and coverage documentation when implemented coverage changes. Report
local checks, live results, and unexecuted scenarios separately.
