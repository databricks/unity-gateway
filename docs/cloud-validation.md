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
exit and timing, checks create/cleanup and reuse retention for both agents, and attempts an unsupported-native-flag
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
| Interactive Codex turn/exit | PASS | Native 0.167.0 returned INTERACTIVE_OK. Test terminal TERM=dumb prompted confirmation; remote desktop tracing warned about missing systemd bus |
| Native Codex interactive resume | PASS | Picker resumed latest conversation; recalled INTERACTIVE_OK without repeating it; clean exit |
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

## Final automated results

- 39 cloud component cases; combined cloud + integration-contract selection: **167 passed**.
- Broad selection: **8,028 passed, 18 failed, 10 skipped** in 462.43 seconds. This selected
  `tests` excluding `integration` and `test_e2e.py`; two other gateway-auth E2E tests were collected
  by this broad command and failed, so this is not a pure offline-unit result.
- All **18 failures reproduced on unchanged upstream main** (`e7290947`) using the same Python
  environment and the exact failing node IDs: 18 failed in 62.72 seconds. No cloud case failed.
- Failure groups: 11 Claude PTY replay tests; 2 Claude smart-routing tests (local prompt socket);
  3 Codex TUI helper startup cases; 2 user-agent gateway tests (missing/stale local auth token).
  These remain unresolved upstream/environment failures; no tests were weakened or marked xfail.
- Ruff lint, Ruff format, and ty type checks passed on the final code.
- Candidate wheel SHA256: `ad28580c3694246fb24f84d2a8b6836f15eefa219f88e36c333c30cb78d701de`.
  Runtime source matches implementation commit `4708d4d1`; subsequent changes update test driver/docs.
- Git push used a per-command override for the user's global DISABLED push rewrite and the existing
  gh credential helper, explicitly authorized by the request to push this PR. Global configuration
  and existing remotes were not modified. The branch was pushed only to the user's fork.

This is broad coverage of the supported paths, not “all possible variations.” Fresh-create lifecycle
remains blocked by current quota; long-running auth renewal, kill/disconnect recovery, concurrent
resume, and other image/platform versions require additional coverage before release.

## Recorded demonstrations

[Four real terminal GIFs](https://github.com/databricks/unity-gateway/pull/1689#recorded-cli-journeys) capture interactive Claude launch/exit,
exact-session picker resume, a headless attempt in an existing environment, and quota
rejection. The latest captures use minimal commands and default routing. Unlike the earlier
headless success with routing disabled, the minimal headless command fails because the
router selects a deprecated DeepSeek endpoint. Interactive Claude recovers with `/model opus`. GIFs show accelerated playback with elapsed-time labels. Their hashes and
process outcomes are recorded in `docs/media/cloud/manifest.json`. Automatic cleanup
could not be filmed because fresh creation remains quota-blocked. No success is simulated.
