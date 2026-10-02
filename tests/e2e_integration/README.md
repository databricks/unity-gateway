# Full E2E critical user journeys

For agent contributors, [AGENTS.md](AGENTS.md) contains the shared Codex and
Claude Code instructions; `CLAUDE.md` imports the same file.

This directory is reserved for complete journeys against real workspaces and
real installed agents, with no stubbed configuration. The existing
`tests/integration/` suite remains separate and includes both real-workspace
and `managed_fixture` cases. No existing tests are moved by this scaffold.

There are currently **zero executable journeys** here. `test_cuj_smart_routing.py`
describes the first planned scenario; it is not a passing or skipped placeholder.
Collection returns pytest exit code 5 (no tests collected), and the runner does
not convert that to success. This scaffold adds no live smart-routing coverage.

## Layout and conventions

- Name every journey module and function `test_cuj_*`. Keep separate top-level
  functions for Claude and Codex, with `Scenario:` and `Expected:` docstrings.
- Keep configure commands, launches, tasks, and assertions visible in the test.
  Fixtures supply fresh environments and credentials, not configured applications.
- Put independently written shared functionality in `helpers/`. Add helpers when
  a journey needs them: processes and cleanup, terminal interaction, file tasks,
  evidence parsing, and workspace lifecycle. Do not create unused implementations.
- Do not import `ucode`, existing `test_e2e*` modules, or
  `tests/integration/utils` helpers. Reusing the installation runner does not
  share test fixtures or helper code.
- Do not use mocks, monkeypatching, config stubs (including
  `UCODE_MANAGED_CONFIG_STUB`), replacement executables, or fake services.
  Create application state through public commands. Do not change production
  code solely to support tests.
- Drive real interactive prompts when the scenario claims TUI coverage. A
  banner, startup, echoed prompt, or tool output alone is not task completion.
- Use the `live` marker, `tui` for interactive journeys, and the appropriate
  `claude` or `codex` marker. Agent selection fixtures will accompany the first
  executable journeys; they are not implemented in this scaffold.

## Run and validate

The suite has its own pytest configuration and requires a fixture boundary to
avoid inheriting the unit suite's autouse mocks. Inspect the scaffold without
installing agents or contacting a workspace:

```bash
uv run pytest -c tests/e2e_integration/pytest.ini \
  --confcutdir=tests/e2e_integration tests/e2e_integration --collect-only
```

Expect exit code 5 until executable journeys are added. Ordinary `uv run pytest`
does not collect this directory. Local contract tests live in
`tests/test_cuj_e2e_contract.py` and runner checks in `tests/test_integration_runner.py`.

Once journeys are implemented, select the suite using the existing installer:

```bash
python scripts/run_integration.py --suite e2e-integration \
  --ug-version checkout --claude-version 2.1.280 --codex-version 0.154.0 \
  --workspace https://dbc-1a9622fc-2e91.cloud.databricks.com \
  --profile YOUR_PROFILE -- -k test_cuj_smart_routing
```

Use a clean disposable POSIX runner and exact versions. Pass authentication
explicitly via a selected profile, `DATABRICKS_BEARER`, or the runner's supported
service-principal credentials. Never choose a developer profile automatically.
The workspace in this example is the planned smart-routing fixture, not an
implicit default. `--installation-only` and `--headless-only` remain options of
the existing `integration` suite and cannot select this suite.

The runner installs isolated versions, sets the pytest boundary, and records the
selected suite, versions, dependencies, JUnit result, and artifacts. Missing
prerequisites, unexpected exits, and empty selections must fail honestly. No
live CI lane is added until there is an executable journey.

## Isolation, concurrency, and evidence

Future helpers must use unique homes, projects, session markers, and artifact
paths. Agent processes need explicit environments, deadlines, and child-process
cleanup on failure. Keep secrets out of recorded commands and artifacts.

Fresh homes do not isolate OS-managed settings: each invocation needs a
disposable machine. The smart-routing journey also changes a workspace-wide
singleton config. Participating runs must hold one cross-machine lock through
setup, tasks, and verified restoration. Check ownership and unexpected policy
changes before publishing or restoring; retain the lock and recovery guidance
if restoration fails. Do not steal locks automatically or blindly restore over
another administrator's changes. Unrelated clients do not honor this lock, so
use a workspace reserved for publication tests.

Poll observable conditions with bounded deadlines. Retry lock contention and
publication visibility only, never failed agent tasks. Correlate decisions and
completed inference turns to unique prompts and sessions, and exclude old
records using per-launch evidence boundaries. Smart routing must not depend on
a particular winning model. Record the limits of native model evidence rather
than claiming independent server-side verification.

The locking, publication, terminal, and evidence helpers are future work. This
PR performs no workspace mutations and no model inference.
