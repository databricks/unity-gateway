# Startup latency audit — 2026-11-10

Base: `222b4105` on `main`. The original checkout contained uncommitted startup
work; this investigation used separate worktrees and did not include or modify
that work. Existing open PRs were checked before implementing more fixes.

## What is on the critical path

A normal configured launch passes through `entrypoint.main`, CLI import and
command construction, `_launch_tool`, bootstrap checks, `configure_shared_state`,
agent configuration, optional skill refresh, and the native launcher. Handoff
is not prompt readiness: the native agent then initializes, loads MCP servers,
performs its own authentication/discovery, and may contact a model.

The following observations come from current source, a fresh-process import
trace, and controlled component measurements. They are not production latency
percentiles. The PR references describe existing work, not changes made here;
their status is as observed on the audit date.

| Cause | Evidence and effect | Existing work / action |
| --- | --- | --- |
| Unconditional Python imports | A local `-X importtime` sample spent 892 ms importing `ucode.cli`; the `ucode.ui` subtree was 301 ms, including 244 ms under Questionary. These nested times overlap and must not be added. CLI setup also imports all agent modules. | Picker and command-import changes: #1380, #1491. Distribution metadata: #1499. |
| Rebuilding the full CLI in helper processes | `auth-token` and per-prompt hooks can pay initialization repeatedly, independently of agent handoff. `--version` and `mcp-proxy` already have specialized entrypoint dispatch. | Credential helper #1384; hook imports #1383; cross-process bearer cache #1541. |
| Installed-binary probes | Databricks discovery scans candidate binaries and runs `--version`; individual probes allow 10 seconds. Native-agent version checks add subprocess startup too. | Databricks persistent version cache #1386; parallel probes #1489; transient probe recovery #1326. Do not remove minimum-version gates. |
| Authentication and refresh | Token subprocesses, cache contention, OAuth refresh and interactive login add variable delay. Current code already memoizes tokens in-process; minting and browser flows remain necessary when credentials are not usable. | Cross-helper token reuse #1541. Keep credential/workspace scoping and explicit refresh semantics. |
| Policy/state reads | A configured launch normally replays saved policy rather than fetching it on a timer. Repeated parsing/selection still occurs in different launch layers. | State-read reuse #1500; resolved-policy reuse #1516. Do not describe every saved-policy lookup as an HTTP call. |
| Probe and UC model discovery | `configure_shared_state` already overlaps the capability probe with the UC listing. Pagination, failures and family fallbacks still extend the critical path. | Recent probe reuse #1389; successful discovery reuse #1421; permanent-error retry handling #1323. |
| Duplicate foundation listing | When both Gemini and Responses are absent from UC discovery, the fallback functions request the same foundation-models URL twice in series. This affects Pi and full multi-agent setup. | Fixed here: derive both requested families from one fresh response, with no persistent cache. |
| Other serial or repeated model reads | Claude fallback, model-picker metadata and provider model lists may require separate requests. | Concurrent family fallbacks #1476; Claude catalog reuse #1324; web-search/probe overlap #1385. |
| Named provider resolution | `resolve_provider_service` lists services before trying a named GET. Codex later needs provider metadata again. | Direct named GET #1492; validated metadata reuse #1504. |
| Budget recommendations | The advisory POST allows 30 seconds and follows discovery on named-agent launches. A failed read falls back to configured defaults. | Shorter advisory timeout #1497; overlapping budget discovery #1503. Do not skip server-side enforcement. |
| Config parsing and writes | Codex parses TOML and constructs a catalog. Shared text/JSON/TOML/dotenv writers also rewrite unchanged files, changing mtimes on each call. | Codex parsing/catalog #1490; separate unchanged-config-write fix and benchmark described in `unchanged-config-writes.md` when available. |
| Native config reconciliation | Claude's active config path and equivalent generated auth helpers affect whether registration is repeated. | Active Claude config #1539; equivalent helper retention #1278. |
| Skill refresh | A due user-downloaded-skill sweep is on the launch path, rate-limited to 24 hours and budgeted to 60 seconds. Individual metadata/file requests and retries can dominate it. | Shared-source reuse #1427; pipelined listings #1434; download/metadata overlap #1514. No change to installed skills here. |
| Codex setup/readiness | Native catalog extraction and app-server readiness are additional subprocess/protocol work, especially under routing. | Catalog/preflight overlap #1518; exited app-server detection #1517. These are separate from Python import latency. |

No unbounded background work, new cross-launch credential/model cache, reduced
authentication checks, disabled managed policy, or default preflight bypass was
introduced. DNS/TLS, gateway load, interactive login/install, native agent boot,
MCP initialization and first-model response remain unmeasured live dependencies.
They cannot be assigned latency savings from these component benchmarks.

## Foundation fallback fix

`discover_endpoints_with_api_types` fetches one response and applies the existing
per-API filtering and diagnostic logic to each requested family. The single-API
entrypoint delegates to it, retaining its signature. The CLI combines discovery
only when both families are requested and both are missing from UC results.
Single-family, complete-UC, provider-only and skip-preflight behavior stays intact.
The normal HTTP transport still supplies authentication and workspace headers.

The result is not memoized: the next discovery reads again, including after an
error or credential change. Version ordering, v2-only filtering and separate
per-family empty-list reasons remain unchanged.

This is complementary to #1476's concurrency work, but both edit the fallback
block. When integrating them, schedule one shared foundation lookup for the two
missing families rather than restoring two concurrent requests to the same URL.

## Controlled measurements

Linux, Python 3.12.3, same interpreter/dependencies, 15 samples after one warmup:

| Operation | Before | After |
| --- | ---: | ---: |
| `configure_shared_state` for Pi with both fallback families, 100 ms per mocked HTTP response | 200.89 ms median | 101.82 ms median |
| Foundation-model requests per invocation | 2 | 1 |

The approximately 49% reduction applies to this controlled component scenario,
not whole-agent startup. The benchmark replaces authentication, probe, UC listing
and persistence with controlled results; it retains the real CLI discovery logic
and family parsing. Filesystem caches, host load and network latency affect real
launches. An all-UC launch avoids the foundation listing in both versions.

Reproduce from this checkout with its Python environment and two source trees:

```sh
uv run --no-sync python scripts/benchmark_foundation_discovery.py --source /path/to/baseline/src
uv run --no-sync python scripts/benchmark_foundation_discovery.py --source src
PYTHONPATH=src python -X importtime -c 'import ucode.cli' 2> /tmp/ug-imports.txt
```

The benchmark isolates home/environment before importing UG and never authenticates
or sends a request. It emits raw samples and source/interpreter metadata as JSON.

## Validation and remaining work

- 1,601 CLI, Databricks, foundation-discovery and integration-contract component
  tests pass with an isolated home and inherited UG settings removed.
- Full Ruff, Ruff formatting and `ty check src/` pass; the benchmark script passes
  Ruff and formatting as well.
- The existing baseline benchmark makes two requests; the changed source makes
  one. Both must return the same model lists for the benchmark to succeed.
- No live workspace, native-agent task, MCP initialization or first-prompt
  measurement was performed. Component passes do not establish integration passes.

The highest-impact next step is to integrate and validate the existing import,
probe, discovery, helper and advisory-timeout fixes together. Then collect live
entrypoint-to-handoff and handoff-to-prompt measurements separately, partitioned
by agent, provider, policy, refresh/preflight mode and cold versus warm setup.
Keep request counts and phase durations alongside total latency so an improved
local component is not mistaken for a gateway or native-agent improvement.
