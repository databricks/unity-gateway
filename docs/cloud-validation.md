# Cloud launcher decisions and validation

## Decisions

- Scope: connected Claude/Codex interactive and headless execution on Linux/macOS using
  Databricks Sandbox CLI plus SSH. Reuse ug workspace resolution; do not hardcode a workspace.
- Preserve native terminal handoff. Allocate a remote PTY with util-linux `script` because the
  tested Sandbox SSH gateway accepted `-tt` but exposed pipes to the remote process.
- Retain interactive and explicitly reused environments. Delete newly created headless environments
  in a finally block; preserve job errors and report failed cleanup. No automatic log/artifact export.
- Repair remote ug's auth mode through public PAT configure using the service-mounted DEFAULT
  profile. Do not copy local credentials or start unusable browser OAuth inside the Sandbox.
- Stop native CLI retries on observed quota rejection. Debug output is inspected in memory only.
  Matching the current error text is an image/CLI compatibility dependency, not a general API contract.
- Exact UUID resume for ordinary Claude sessions; latest-session resume for Codex. Metadata is local
  and scoped by workspace/profile/agent/directory. Explicit native resume requires `--sandbox`.
- Reject detached/nested cloud flags until their lifecycle is implemented. No claim of disconnect
  survival, native background job support, live resize, or shared-host workspace support.
- Keep the PR independent of the Auto picker and Omnigent work in the original dirty checkout.
  Built from upstream main `e7290947bd8ed566467b1a7cdc8680131d55f869` in a separate worktree.
- Publish as a proposed/draft PR with validation gaps visible. No release or deployment to ordinary ug.

## Reproducible checks

```bash
uv sync --frozen
uv run pytest -q tests/test_cloud.py tests/test_integration_contract.py
uv run pytest -q tests --ignore=tests/integration --ignore=tests/test_e2e.py
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run ty check src/
```

Opt-in live lifecycle driver (real installed CLI, service and models; creates billable environments):

```bash
python tests/cloud_live.py --ug /path/to/ug --profile YOUR_PROFILE \
  --claude-model YOUR_CLAUDE_MODEL --codex-model YOUR_CODEX_MODEL --output /tmp/cloud-evidence
```

The driver fails on unavailable services rather than skipping. It records each command's output,
exit and timing, checks create/cleanup and reuse retention for both agents, and attempts an invalid-model
failure cleanup. It cleans up only IDs it created. It is a standalone live driver, not an addition to
all existing integration-runner lanes. Terminal journeys require actual PTY interaction separately.

## Current branch live validation (November 15, 2026)

Client: Ubuntu 24.04 ARM64, Python 3.12.3, native Databricks CLI 1.23.0.
Installed candidate wheel: `0.2.1+cloudpr.1`. Service/image versions are independent of this wheel.

| Variation | Result | Evidence / scope |
|---|---|---|
| Offline cloud help | PASS | Installed candidate lists commands without provisioning |
| Fresh Sandbox creation | BLOCKED by service quota | RESOURCE_EXHAUSTED, configured limit 200; exit 1 in 2.29 seconds |
| Quota error visibility / stopping retries | PASS live | Above rejection surfaced promptly, no indefinite “Creating…” |
| Existing Sandbox, headless Claude | PASS | Python sum 60, CLOUD_OK_60, exit 0; environment retained |
| Existing Sandbox, headless Codex | PASS | Recorded native Python execution output 60, CLOUD_OK_60, exit 0 |
| Fresh headless cleanup / failure cleanup | BLOCKED live by quota | Component coverage; prior preview live success is historical only |
| CLI launch and terminal exit, interactive Claude | PASS | Real TUI, accepted trust for dedicated test directory; marker turn completed; exact-ID footer |
| Interactive Claude picker resume | PASS | Reopened same UUID, recalled cobalt-73 without repeating marker; both exits 0 |
| Model selection | Policy redirected Claude | Requested Opus 5; organization selected its allowed Opus 5.5. Do not count this as proof of Opus 5 inference |
| Native Codex interactive resume | NOT RUN | Headless Codex and component argument forwarding do not prove TUI resume |
| Windows / live resize / disconnect survival | NOT RUN / unsupported | Outside supported initial scope |

The existing user's Sandbox was reused and retained. No unrelated resources were deleted.
MCP authentication warnings were observed in the remote image but MCP was not part of these tasks.
Raw evidence is kept locally, not committed: it can contain environment-specific details. The table
records observations rather than representing itself as a raw transcript. No credentials are committed.

## Component matrix

Quoting and prompt forwarding; explicit profile credential isolation; workspace mismatch; headless
versus PTY transport; terminal allowlist; create failure; quota rejection/reaping; automatic cleanup;
cleanup failure recovery; interrupted headless cleanup; reused-environment retention; exit status;
help without side effects; deletion confirmation; missing/mismatched remote credentials; auth repair;
failed repair; resume/fresh/new/cancel picker choices; deleted environment; onboarding without saved
conversation; scope isolation; exact Claude UUID; explicit resume without Sandbox; rejected background
flags; footer shell quoting and exit status. Mock/subprocess fixtures are component evidence only.
