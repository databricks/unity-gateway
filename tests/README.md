# Test suites and installation checks

Existing unit/component tests live in `test_*.py`; the existing e2e tests exercise
the workspace with some patched setup. The separate `integration/` harness runs
an installed ug wheel or exact release through subprocesses with real pinned
agent binaries. It does not import application internals or use test doubles.

## Installation coverage

| Test | Scenario | Expected evidence |
| --- | --- | --- |
| `test_ug_installed_wheel_exposes_help_and_version` | Invoke the freshly installed console command | Correct package version and working help |
| `test_ug_status_in_fresh_home_is_unconfigured` | Request status in a fresh home | Unconfigured status |
| `test_ug_auth_without_configuration_explains_how_to_configure` | Request auth before configure | Actionable setup error and nonzero exit |

Only these three installation cases are currently defined. The shared harness
also supplies isolated live-session fixtures, PTY control, transcript evidence,
and bounded process cleanup for live journeys.

Run package checks with `scripts/run_integration.py --installation-only` and
explicit ug/agent versions. See [integration/README.md](integration/README.md)
for the full command and artifacts. Package checks do not claim live model
coverage. Live workspace behavior, interactive tasks, and routing journeys
require their own tests and completed answers.

Follow [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md) when adding tests.
The ordinary suite checks the subprocess-only boundary and requires
Scenario/Expected docstrings on integration cases.
