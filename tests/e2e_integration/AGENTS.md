# Full E2E CUJ instructions

These instructions are shared by Codex and Claude Code. Read [README.md](README.md)
for the suite's execution, isolation, concurrency, and evidence requirements before
adding or changing a journey. `CLAUDE.md` imports this file; keep the policy here.

## Adding journeys

- Name every journey module and function `test_cuj_*`. Keep tests at this directory's
  root, with separate explicit Claude and Codex functions and `Scenario:` / `Expected:`
  docstrings. Keep configure, launch, task, and assertions visible in each test.
- Put independently written reusable functionality in `helpers/`. Fixtures supply
  isolated environments and credentials, not preconfigured application state.
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
- Do not hide failures with skips, xfails, task retries, or weaker assertions.
  Do not add production behavior solely for tests. Keep secrets out of artifacts.

## Validation and reporting

Run live journeys through `scripts/run_integration.py --suite e2e-integration`
with exact versions and explicit workspace/authentication. Direct collection needs
this suite's `pytest.ini` and `--confcutdir=tests/e2e_integration`; never inherit the
unit suite's autouse mocks. Commands in the README run from the repository root.

Keep local helper/contract tests in the ordinary unit suite. Update the README and
coverage documentation when implemented coverage changes. Report local checks,
live results, and unexecuted scenarios separately.

The current suite is scaffolding only: `test_cuj_smart_routing.py` specifies future
journeys and collects no tests. Do not add passing or skipped placeholders, or
convert empty collection into a successful E2E run.
