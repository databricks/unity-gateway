# CLI startup latency

Updated November 5, 2026. Experimental baseline: `83d9e3b5` on `main`.
The goal is a substantial reduction in startup p90 for affected launch cohorts,
with unchanged authentication, workspace selection, and model-selection behavior.
A production UG-only p90 baseline still needs a representative set of launch measurements.

## Measurement boundaries

| Metric | Start → finish | What it establishes |
| --- | --- | --- |
| UG setup | UG entry → agent process handoff | Time UG spends preparing a launch; excludes preceding interpreter startup and subsequent agent initialization |
| Process startup | UG process creation → agent process handoff | User-visible wrapper cost, including interpreter startup |
| Agent readiness | Launch request → usable native agent prompt | Includes launcher and agent initialization; requires observing the real agent |
| First response | Prompt submission → first model output | Includes inference; separate from startup |
| Help, version, helper commands | Process creation → command exit | Fixed command overhead; neither help nor version proves an agent is ready |

A successful handoff is not a successful agent task. Keep each boundary explicit
in results; do not add overlapping phase percentiles or subtract cohort percentiles
to estimate UG's contribution. Label mocked requests and stubbed agents.

## Current evidence

Three independent changes address repeated work or waiting on current `main`.
Their benchmarks run real component code with controlled external costs. They do
not measure production scenario frequency or complete launch latency.

| Fix | Trigger and work removed | Controlled p90 before → after | PR |
| --- | --- | --- | --- |
| Reuse an unavailable budget recommendation | Bare `ug` previously repeated an empty/failed budget lookup; one lookup and warning now suffice for that launch | 101.70 → 51.58 ms, 50 ms modeled request | [#1474](https://github.com/databricks/unity-gateway/pull/1474) |
| Overlap missing-family discovery | Missing requested families waited for earlier fallback reads; independent reads now overlap | Claude: 100.50 → 50.78 ms; Pi: 150.59 → 50.89 ms, 50 ms per modeled read | [#1476](https://github.com/databricks/unity-gateway/pull/1476) |
| Avoid an unnecessary Claude config lock | An already-enabled hooks gate waited behind a writer even though no update was needed | 50.46 → 0.21 ms, 50 ms modeled lock hold | [#1475](https://github.com/databricks/unity-gateway/pull/1475) |

Numbers above use 50 measured samples per revision and scenario, alternating
baseline/candidate order, after warmup, with nearest-rank p90. Imports and fixture
setup are outside these component timers. The PRs record tested revisions and
include scripts that emit raw samples; the measured sample files are retained
locally. Gate updates still take the lock and reread the latest file; their
contended p90 was 50.97 → 51.05 ms. The fallback change preserves request counts,
including separate Gemini and Codex reads of the foundation-models endpoint.

These savings apply to different conditions and must not be summed. A useful
overall p90 claim still requires representative launch measurements after the
selected fixes are combined, plus functional validation with real agents.
The combined code at `99165f2c` passed 2,199 component/contract tests and the
repository lint gate. This does not establish live agent or gateway coverage.
The new PRs are drafts; check their current hosted checks before landing.

## Reproduce component experiments

Use the benchmark from the corresponding fix branch and the same Python 3.12+
environment/dependency lock for baseline and candidate. These experiments require
no workspace credentials and do not launch an agent.

```sh
uv sync --frozen
uv run --frozen scripts/benchmark_startup_budget.py --baseline ../ug-baseline --runs 50
uv run --frozen scripts/benchmark_model_fallbacks.py --baseline ../ug-baseline --samples 50
uv run --frozen scripts/benchmark_claude_hooks_gate.py --source ../ug-baseline --runs 50
uv run --frozen scripts/benchmark_claude_hooks_gate.py --runs 50
```

The budget and family benchmarks alternate revisions themselves. For a paired
gate comparison, invoke its script with `--runs 1` for each revision in 50 or more
rounds, alternating baseline/candidate and candidate/baseline order. Aggregate the
retained samples per scenario; two sequential 50-run batches are only a smoke check.
Keep `--delay-ms` / `--lock-hold-ms` identical across revisions and report their values.

## Repeatable launch comparisons

1. Record baseline and candidate commit IDs, dirty-tree status, Python/dependency
   versions, agent versions, platform, command shape, and measurement boundary.
2. Create isolated equivalent configurations through public CLI commands. Compare
   the same agent, workspace, policy, and routing settings. Keep first configuration,
   configured launch, warm/cold authentication, due/not-due skill refresh, and
   discovery-skipping/discovery-required cohorts separate.
3. Use at least 50 measured attempts per revision and cohort after documented
   warmups. Alternate AB/BA order on the same host. Retain unsorted raw durations,
   outcome labels, timeout limits, and the measured order. Exclude setup from the
   timer only when the named metric excludes it.
4. Report p50, nearest-rank p90 (`sorted[ceil(0.9*n)-1]`), maximum, successful sample
   count, and total attempts. Report failures, timeouts, cancellations, and missing
   handoff/readiness markers separately. A timeout is censored, not a fast success;
   do not improve a percentile by silently dropping failed attempts.
5. Fifty samples give an initial tail estimate, not a stable production percentile.
   Repeat batches and increase samples when variance or cohort changes obscure the
   result. Do not assert significance from a small point estimate alone.
6. Verify the selected fixes together with focused regressions and real agent
   journeys. Publish measured improvements only for the exercised cohorts, and
   record untested platforms, authentication modes, and readiness boundaries.

## Existing PRs and order

The links describe work in flight; check current reviews and CI before landing.
Fix merge conflicts against the actual base and measure the resulting stack.

| Priority | Work | Dependency or decision |
| --- | --- | --- |
| Measurement | [#1292](https://github.com/databricks/unity-gateway/pull/1292), [#1465](https://github.com/databricks/unity-gateway/pull/1465) | Phase diagnostics and base/head benchmarking; #1465's small default sample is a smoke check, not a production p90 gate |
| Bounded startup work | New fixes above; [#1380](https://github.com/databricks/unity-gateway/pull/1380), [#1386](https://github.com/databricks/unity-gateway/pull/1386) | Defer picker imports and reuse CLI version discovery; preserve fallback and invalidation behavior |
| Repeated reads | [#1322](https://github.com/databricks/unity-gateway/pull/1322), [#1323](https://github.com/databricks/unity-gateway/pull/1323), [#1324](https://github.com/databricks/unity-gateway/pull/1324), [#1385](https://github.com/databricks/unity-gateway/pull/1385) | Policy/catalog reuse, permanent-error retry policy, and web-search overlap; retest their shared launch path together |
| Recurring helpers | [#1383](https://github.com/databricks/unity-gateway/pull/1383) → [#1384](https://github.com/databricks/unity-gateway/pull/1384) → [#1387](https://github.com/databricks/unity-gateway/pull/1387) → [#1390](https://github.com/databricks/unity-gateway/pull/1390) | Respect the helper/import stack; coordinate [#1388](https://github.com/databricks/unity-gateway/pull/1388)'s token reuse with hook changes |
| Due skill refresh | [#1427](https://github.com/databricks/unity-gateway/pull/1427), [#1434](https://github.com/databricks/unity-gateway/pull/1434), [#1436](https://github.com/databricks/unity-gateway/pull/1436) | Shared sources, directory scheduling, attribution indexing; measure due-refresh launches separately |
| Cross-launch caching | [#1389](https://github.com/databricks/unity-gateway/pull/1389) **or** [#1421](https://github.com/databricks/unity-gateway/pull/1421) | Alternative policies, not additive fixes: 15-minute discovery-skipping reuse versus broader five-minute discovery reuse; choose freshness/invalidation semantics before combining |
| Coordinate first | [#1382](https://github.com/databricks/unity-gateway/pull/1382) | Reconcile scoped-probe reuse with already-merged [#1372](https://github.com/databricks/unity-gateway/pull/1372)'s overlap before promoting |

After each merge, update this table, the tested revision, benchmark result, and
remaining gap. Next priorities should follow the measured slow-launch cohorts,
including their failed or incomplete attempts.
