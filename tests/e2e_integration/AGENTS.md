# Full E2E CUJ instructions

These instructions are shared by Codex and Claude Code. `CLAUDE.md` imports this
file; keep all suite guidance here.

This suite contains full journeys against real workspaces and installed agents,
without stubbed configuration. The existing `tests/integration/` suite remains
separate, including its `managed_fixture` cases. No existing tests have moved.

## Primary requirement: one Databricks workspace per test

Every test invocation must have its own exclusively assigned Databricks workspace.
This means a distinct workspace identity, not just a different local directory,
schema, config name, or session. Independently collected tests get separate
workspaces; parallel workers, developers, and CI runs must not share an assigned
workspace. A single cross-agent CUJ may exercise both agents in its own workspace.

Use function-scoped allocation and hold ownership through setup, all phases of
the journey, and verified cleanup. Each test sets up its required real configuration
in its own workspace and must run independently, without relying on another test's
state or execution order. A pooled workspace may be reassigned only after cleanup
is verified; quarantine it on cleanup failure and report recovery information.

A lock serializing tests against one shared workspace does not meet this requirement.
Do not fall back to a shared workspace when allocation is unavailable: fail before
configuration or inference. Record each test's workspace identity in its artifacts,
without credentials. Acquire the allocation before configuring or serving tasks.

`tests/test_cuj_smart_routing.py` implements one cross-agent CUJ with six fresh
interactive sessions: routed, explicit supported model, and routing-disabled for
each agent. It uses the already-published model lists/defaults, not fixed router
winners. No MPS, budget policy, or tracing participates. Both routing flags are
disabled in one publication, then the same home is reconfigured. Teardown restores
the original publication. Empty selections are failures, not successful E2E runs.

`helpers/workspace.py` claims the class's dedicated workspace via a create-only
notebook at `/Shared/ug-e2e-workspace-reservation`. This is a fail-fast exclusive
assignment, not a queue serializing different tests onto a shared workspace.
There is no automatic provisioning, pool, TTL, lock stealing, or global fallback.
Contention fails before configure/inference. Concurrent users need separately
assigned workspace URLs declared on their CUJ classes. Each runner must itself be
disposable and exclusive because agents also write machine-wide settings.

The reservation coordinates this suite's runners, not arbitrary administrators.
Do not edit a reserved workspace. The config API has no CAS; read-back checks
detect observed external edits but cannot eliminate the read/write race with
non-cooperating writers. Failed/ambiguous restoration retains the reservation
and writes `QUARANTINED` to the workspace artifact. After a crash, inspect the
owner, original scenario, and live config; restore/verify the policy and stop any
old runner before an administrator removes that exact reservation. Never auto-clear it.

## Adding journeys

Keep journey modules in `tests/`, reusable code in `helpers/`, and pytest
configuration and shared instructions at the suite root:

```text
e2e_integration/
├── AGENTS.md
├── CLAUDE.md
├── conftest.py
├── pytest.ini
├── helpers/
└── tests/
    └── test_cuj_smart_routing.py
```

Derive each CUJ class from `BaseCujTest` and declare its workspace explicitly:

```python
from helpers.base import BaseCujTest


class TestCujSmartRouting(BaseCujTest):
    WORKSPACE_URL = "https://dbc-1a9622fc-2e91.cloud.databricks.com/"
```

Journey methods use `self.workspace_url`, which returns the class's `WORKSPACE_URL`
directly. Collection rejects missing URLs and duplicate workspace URL declarations
across collected tests; it does not validate or normalize URLs.
Independently collected scenarios need separate
classes and workspaces, including parametrized cases. This check does not provision
workspaces or establish exclusive ownership across processes; the function-scoped
`cuj` fixture acquires the remote reservation before any configure or inference.
It verifies that the runner's explicit credential target matches the class URL.

- Name every journey module and test method `test_cuj_*` and classes `TestCuj*`.
  Keep tests in `tests/`, with explicit agent scenarios and `Scenario:` / `Expected:`
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

Expect exactly one collected cross-agent smart-routing test. Always use the
suite's own pytest configuration and fixture boundary; never inherit the unit
suite's autouse mocks. Ordinary `uv run pytest` excludes this directory.

Run live journeys through
`scripts/run_integration.py --suite e2e-integration` on a clean disposable POSIX
runner with exact versions and explicit authentication. Never select a developer
profile automatically. The runner's single `--workspace` input binds credentials
to the selected class URL; it cannot reassign a test to a shared workspace. Run
different workspace classes in separate runner invocations, each with matching
explicit credentials and its own disposable host.
`--installation-only` and `--headless-only` belong to the existing `integration` suite.

The runner records the selected suite, versions, dependencies, JUnit results,
and artifacts. Missing prerequisites and empty selections fail honestly. Add no
live CI lane until this CUJ has been validated on the intended clean runner.

For this CUJ, supply both agents (selecting only one is an error):

```bash
python3.12 scripts/run_integration.py --suite e2e-integration \
  --ug-version checkout --claude-version 2.1.280 --codex-version 0.154.0 \
  --workspace https://dbc-1a9622fc-2e91.cloud.databricks.com \
  --profile dbc-1a9622fc-2e91 -- -k test_cuj_smart_routing
```

Prerequisites include config read/update rights, create/export/delete permission
for the reservation, live model catalogs/inference, and an ordinary functioning
agent sandbox. Do not disable the sandbox to make containers pass. Existing
machine-wide settings cause a preflight failure, not an overwrite.

Evidence combines per-launch log boundaries, the sole submitted prompt, native
parent-session records, the final answer's unpredictable file value, and normal
process exit. Claude uses assistant response model metadata; Codex links the
prompt to a completed native turn and its model context. This is client execution
evidence, not independent server-side inference telemetry. Claude's first-prompt
log does not contain the prompt: its correlation relies on the isolated fresh
session and exactly one submitted prompt. Successful router logs plus an
independently queried live `system.ai` catalog establish target support; banners
are never evidence of application. Artifacts include redacted terminal/native
records, model inputs/results, and workspace ownership/recovery state.

Keep local helper/contract tests in the ordinary unit suite. Current checks live
in `tests/test_cuj_e2e_contract.py`,
`tests/test_cuj_evidence.py`, `tests/test_cuj_workspace.py`, `tests/test_cuj_session.py`, and
`tests/test_integration_runner.py`. Update
this file and coverage documentation when implemented coverage changes. Report
local checks, live results, and unexecuted scenarios separately.
