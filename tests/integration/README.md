# Installed-package integration harness

This harness installs a fresh ug wheel or exact release into its own virtualenv
and exact Claude/Codex versions into an isolated npm prefix. Pytest, pexpect, and
pyte run from a separate test environment. It never imports the application
under test or borrows the checkout's dependencies to make an install succeed.

The three current tests check installed help/version, unconfigured status, and
the auth error before setup. They require no workspace credentials. Live-session,
terminal, and evidence helpers are available, but no live journeys are defined yet.

## Run installation checks

Prerequisites: Python 3.12+, uv, and Node/npm.

```bash
python3.12 scripts/run_integration.py \
  --ug-version checkout --claude-version 2.1.268 --installation-only
```

Always select `--installation-only` for the current package checks. The runner's
default `live` selection needs live tests; selecting an empty suite fails.

`checkout` builds a wheel and resolves fresh consumer dependencies instead of
using `uv.lock`. An exact `--ug-version` reproduces a release; `--ug-wheel`
replays an archived wheel. Older releases may need `--entry-point ucode`.
Agent versions must be exact. Select one agent by passing only its version.

Use `--dependency PACKAGE==VERSION` to constrain a suspected dependency,
`--constraints dependencies.txt` to replay Python dependencies, and
`--npm-lock npm-lock.json` to replay agent dependencies. Locks must match the
original OS and architecture. `--default-index` and `--npm-registry` select mirrors.

The separate pytest configuration and `--confcutdir` prevent the unit fixtures
from leaking into integration. The default unit-test command does not collect
this directory. Selection after `--` accepts `-k`, `-m`, `-x`, and `--maxfail`;
report destinations and pytest configuration cannot be overridden.

## Isolation and evidence

Every test gets a fresh home and working directory outside the checkout.
Commands have explicit stdin, deadlines, redacted diagnostics, and process-group
cleanup. Configured sessions revert through the real public CLI before teardown.
The runner refuses machine-wide agent settings for live runs because those can
override the chosen workspace; it does not edit or bypass those settings.

Each invocation creates a new results directory, by default under
`.integration-runs/`, containing:

- `versions.json`: requested and observed versions, source/wheel hashes, and results.
- `dependencies.txt`, `npm-lock.json`, and `test-dependencies.txt`: dependency evidence.
- `junit.xml`: exact outcomes.
- `artifacts/`: redacted command diagnostics.
- `install.log` and `wheels/`: installation diagnostics and the built wheel.

Homes are removed after each test. Installed environments and dependency caches
remain in the results directory for inspection.

## Live harness and Linux reproduction

Live fixtures require the existing e2e `UCODE_TEST_WORKSPACE` and
`DATABRICKS_BEARER`, or an explicitly selected `--profile` for the runner to mint
a bearer. No developer profile is selected automatically. Databricks CLI >=1.0.0
is also required for live runs.

`utils/harness.py` drives public commands and the real app-server protocol.
`utils/terminal.py` supplies PTY/screen handling and visible onboarding choices.
`utils/evidence.py` reads completed agent answers and routing records. These
helpers do not fabricate application state, responses, or onboarding completion.

The optional Docker image provides a pinned Linux toolchain. Build it from the
repository root, then run an exact published release or mount a wheel:

```bash
COPYFILE_DISABLE=1 tar --format=ustar --exclude=__pycache__ --exclude=.pytest_cache \
  -cf - scripts/run_integration.py tests/integration | \
  docker build -f tests/integration/utils/Dockerfile -t ug-integration -
docker volume create ug-integration-results
docker run --rm --init -v ug-integration-results:/results ug-integration \
  --ug-version YOUR_RELEASE_VERSION --claude-version 2.1.268 --installation-only
```

Each run needs a new output directory. Installation, collection, and lint results
are not evidence of a live integration pass. Follow [../AGENTS.md](../AGENTS.md)
for the integration test contract.
