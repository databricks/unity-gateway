# UG startup latency

This tracks work to reduce the p90 time from starting a configured `ug claude`,
`ug codex`, or bare `ug` process to handing control to the agent. First configure,
interactive agent readiness, and first-response latency are separate measurements.

Status snapshot: **November 5, 2026**, main `83d9e3b5`. PR links below are the
source of truth for current status. Local measurements are Linux x86_64,
Python 3.12.3; they do not establish production or Windows/macOS p90.

## Measurement contract

- Keep interactive PTY and redirected-output results separate. Redirecting output
  disables spinners and misses their startup cost.
- Report warm configured launches, cold authentication/discovery, and due skill
  refreshes separately. Include explicit-agent and bare managed launches.
- Pin both commits, Python/dependencies, native agent versions, workspace policy,
  auth mode, and cache state. Record sample count, failures, p50, p90, and raw
  durations. Exclude installation and `uv sync` from a launch measurement.
- For local comparisons use at least 50 measured samples after warmups,
  interleave base/head order, and calculate nearest-rank p90. Small preliminary
  samples remain labeled exploratory. Never remove failed or timed-out attempts
  silently from the denominator.
- Use the same launch boundary for comparisons. Process handoff does not establish
  that the native agent is interactive. Parent-process timings may include wrapper,
  installation, and host work outside UG. Nested phase percentiles overlap and
  cannot be added or subtracted to infer causal savings.
- Keep credentials, environment dumps, prompts, and personal config out of artifacts.

The working target is a **50% reduction in configured-launch UG p90** against a
pinned baseline, with unchanged launch success and model/configuration behavior.
This is a target, not an achieved result. A current representative end-to-end UG
baseline and the combined candidate measurement remain outstanding.

## Current measurements

The current series addresses repeatable delays. Its component savings must not
be summed into a production improvement.

| Issue | Component experiment | Before → after p90 | Status |
| --- | --- | --- | --- |
| Spinner completion waits for the next 100 ms frame | Actual PTY, 60 samples after 3 warmups, one no-op operation | 100.399 → 0.151 ms | [#1472](https://github.com/databricks/unity-gateway/pull/1472) |
| Repeated spinner waits across steps | Same experiment, four sequential no-op operations | 402.357 → 0.594 ms | Same spinner fix |
| Bare `ug` repeats an empty/failed budget read | 50 interleaved samples; controlled 50 ms unavailable-request delay | 101.70 → 51.58 ms; 2 requests → 1 | [#1474](https://github.com/databricks/unity-gateway/pull/1474) |
| Already-enabled Claude hooks gate waits on a writer | 50 interleaved samples; real local lock held for 50 ms | 50.462 → 0.210 ms | [#1475](https://github.com/databricks/unity-gateway/pull/1475) |
| Missing model-family fallbacks run serially | 50 interleaved fresh-process samples; 50 ms per missing family, two families | 100.503 → 50.776 ms | [#1476](https://github.com/databricks/unity-gateway/pull/1476) |

The spinner uses an interruptible event wait without changing animation cadence
or exception cleanup. The recommendation change distinguishes a completed empty
result from an omitted result; explicit-agent launches still fetch once, and admin
defaults remain intact. A recommendation request uses a 30-second socket timeout;
that is not a strict whole-request deadline.

A fresh-process local baseline on `83d9e3b5` gives these lower-level costs:

| Path | p50 | p90 | Samples |
| --- | ---: | ---: | ---: |
| Version entry point | 16.3 ms | 20.7 ms | 50 |
| Help | 477.3 ms | 546.8 ms | 50 |
| Auth helper with a supplied dummy bearer | 395.9 ms | 442.1 ms | 50 |

These use isolated homes, redirected output, three warmups, alternating command
order, and the same installed dependencies. They make no network requests and
do not launch an agent. They show remaining command/import overhead, not a
configured-launch baseline. The version fast path does not establish CLI health.

## Ordered PR queue

Prioritize eliminating repeated work before changing cache freshness or policy
semantics. Existing PRs already cover most known startup paths; extend those
changes instead of creating competing implementations.

| Priority | Work | Existing PRs and next check |
| --- | --- | --- |
| 1 | Establish UG phase timing and repeatable launch comparison | [#1292](https://github.com/databricks/unity-gateway/pull/1292) adds opt-in local phases; [#1109](https://github.com/databricks/unity-gateway/pull/1109) adds parent milestones; both draft/conflicting at this snapshot. [#1465](https://github.com/databricks/unity-gateway/pull/1465) adds a PR benchmark, draft/mergeable. Its default five runs and redirected output do not cover a representative interactive tail. |
| 2 | Remove spinner completion waits and duplicate budget reads | [#1472](https://github.com/databricks/unity-gateway/pull/1472) and [#1474](https://github.com/databricks/unity-gateway/pull/1474); regression tests and controlled before/after evidence are complete. Live combined launch validation remains outstanding. |
| 3 | Avoid repeated failed policy/catalog reads and permanent-error retries | [#1322](https://github.com/databricks/unity-gateway/pull/1322), [#1324](https://github.com/databricks/unity-gateway/pull/1324). Both conflict with main; #1324 remains draft. [#1323](https://github.com/databricks/unity-gateway/pull/1323) addresses permanent-error retries. Revalidate request counts after resolving conflicts. |
| 4 | Reduce fixed import/process and no-op lock costs | [#1380](https://github.com/databricks/unity-gateway/pull/1380) defers picker imports; [#1386](https://github.com/databricks/unity-gateway/pull/1386) caches Databricks CLI versions. Both conflict. [#1475](https://github.com/databricks/unity-gateway/pull/1475) skips the no-op Claude hooks gate lock. [#1383](https://github.com/databricks/unity-gateway/pull/1383), [#1384](https://github.com/databricks/unity-gateway/pull/1384), [#1387](https://github.com/databricks/unity-gateway/pull/1387), [#1390](https://github.com/databricks/unity-gateway/pull/1390) address helpers/hooks/MCP startup; these may run during native initialization or per turn after UG handoff. Validate their combined import paths and measure that boundary separately. |
| 5 | Overlap independent reads and reuse recent gateway results | [#1476](https://github.com/databricks/unity-gateway/pull/1476) overlaps missing-family fallbacks. [#1385](https://github.com/databricks/unity-gateway/pull/1385) overlaps web-search discovery. [#1389](https://github.com/databricks/unity-gateway/pull/1389) and [#1421](https://github.com/databricks/unity-gateway/pull/1421) are overlapping draft cache approaches; choose one coherent freshness/auth-invalidation policy. These conflict with main. |
| 6 | Reduce the due skill-refresh tail | [#1427](https://github.com/databricks/unity-gateway/pull/1427) fetches shared sources once; [#1434](https://github.com/databricks/unity-gateway/pull/1434) pipelines directory listing. Both mergeable at this snapshot. Measure a real due refresh separately from the daily fast path. |

[#1382](https://github.com/databricks/unity-gateway/pull/1382), scoped gateway
preflight, remains draft: its interaction with the already-merged discovery
overlap needs request-count validation. [#1441](https://github.com/databricks/unity-gateway/pull/1441)
changes launch-time OS-managed writes and needs its own configuration-contract
review. Neither is included in the current four-fix series.

Already merged: [#1017](https://github.com/databricks/unity-gateway/pull/1017)
token memoization, [#1018](https://github.com/databricks/unity-gateway/pull/1018)
version/import fast paths, [#1291](https://github.com/databricks/unity-gateway/pull/1291)
in-process CLI discovery reuse, and [#1372](https://github.com/databricks/unity-gateway/pull/1372)
overlapping model-service discovery. Keep them in the baseline, not in claimed
new savings.

## Verification and follow-up

The spinner fix passed 219 UI/startup/integration-contract component tests. The
other PRs report 1,214 recommendation-related tests, 549 hooks-gate tests, and
1,461 fallback-discovery tests. All report repository Ruff, formatting and type
checks, with regressions that fail on the baseline. The selections overlap; do
not add them into a unique test count. These checks do not establish live
native-agent coverage. Each PR records its exact validation limits.

[#1473](https://github.com/databricks/unity-gateway/pull/1473) independently fixed
the same budget-read issue and was closed in favor of #1474, which includes a
maintained interleaved benchmark. Keep one implementation in the series.

For each landed fix, record the merge/release commit, rerun the corresponding
component regression, then compare the same real configured-launch cohorts at
the same boundaries. After the phase-timing change lands, use
`UG_STARTUP_TIMING=1` to attribute remaining startup waits through handoff.
Validate actual interactive readiness and a completed native task separately.
Do not interpret fewer HTTP calls or a fast `--version` as proof of a working
agent session.
