# Startup latency audit

This audit follows normal agent launches from UG's Python entrypoint to the
native-agent handoff. The reference is `main` at `222b4105` (2026-11-10).
It distinguishes work present there from fixes already proposed in other PRs.

## Critical path

```text
outer launcher / Python initialization
  -> entrypoint dispatch
  -> full CLI imports and Typer command construction
  -> workspace selection, saved state, and workspace policy
  -> dependency/version checks and any first-run setup
  -> authentication and token retrieval
  -> UC model discovery || gateway preflight
  -> missing-family fallbacks and shared-state persistence
  -> budget recommendation and source/model resolution
  -> agent configuration, catalog, hooks, and downloaded-skill refresh
  -> native-agent handoff
  -> agent initialization, token helpers, routing hooks, MCP startup, readiness
```

The ordering varies with the command and configured model source. A bare `ug`
checks the budget before choosing its agent; an explicit agent command can check
it later. A static managed model list, provider, or another writer's Claude
picker can suppress family discovery. `--skip-preflight` bypasses shared-state
authentication validation, gateway probing, and model discovery; it does not
remove every policy, source, helper, or native-agent operation.

The relevant orchestration is in [cli.py](../src/ucode/cli.py), particularly
`_launch_tool` and `configure_shared_state`. Authentication and discovery live in
[databricks.py](../src/ucode/databricks.py); dispatch and agent configuration are
in [agents/__init__.py](../src/ucode/agents/__init__.py).

## Findings and existing fixes

Large delays come from repeated remote/subprocess work and serialized independent
operations. Import costs also recur whenever the native agent starts a UG helper.
The following PRs were open when this audit was written; their presence does not
mean the fixes are available on main or establish their independent speedup.

| Cause on the reference main | Proposed fix |
| --- | --- |
| Profile enumeration validates saved profiles although callers need their metadata | This change requests `auth profiles --skip-validate`; selected-workspace authentication remains in the normal authentication path |
| Permanent model-service errors consume the same retries as transient errors | [#1323](https://github.com/databricks/unity-gateway/pull/1323) |
| Gateway preflight performs an unscoped listing alongside scoped model discovery | [#1382](https://github.com/databricks/unity-gateway/pull/1382) |
| Repeated gateway/discovery work across launches | [#1389](https://github.com/databricks/unity-gateway/pull/1389), [#1421](https://github.com/databricks/unity-gateway/pull/1421) |
| Credential, routing, and stdio-server helpers import the full CLI | [#1384](https://github.com/databricks/unity-gateway/pull/1384), [#1387](https://github.com/databricks/unity-gateway/pull/1387), [#1390](https://github.com/databricks/unity-gateway/pull/1390) |
| Interactive picker dependencies load on ordinary noninteractive paths | [#1380](https://github.com/databricks/unity-gateway/pull/1380) |
| Missing model families load sequentially; Gemini/Codex repeat a foundation-model listing | [#1476](https://github.com/databricks/unity-gateway/pull/1476), [#1498](https://github.com/databricks/unity-gateway/pull/1498) |
| Distinct Databricks executables are version-probed sequentially; new processes repeat probes | [#1489](https://github.com/databricks/unity-gateway/pull/1489), [#1386](https://github.com/databricks/unity-gateway/pull/1386) |
| Resolving one named provider first lists every provider | [#1492](https://github.com/databricks/unity-gateway/pull/1492) |
| Advisory budget retrieval is serial and has a 30-second request timeout | [#1503](https://github.com/databricks/unity-gateway/pull/1503), [#1497](https://github.com/databricks/unity-gateway/pull/1497) |
| Launch preparation rereads policy, provider metadata, and Claude catalogs | [#1516](https://github.com/databricks/unity-gateway/pull/1516), [#1504](https://github.com/databricks/unity-gateway/pull/1504), [#1324](https://github.com/databricks/unity-gateway/pull/1324) |
| Managed Codex catalog extraction starts after independent preflight work | [#1518](https://github.com/databricks/unity-gateway/pull/1518) |
| An exited Codex app-server still consumes its readiness wait | [#1517](https://github.com/databricks/unity-gateway/pull/1517) |
| Read-only inspections use formatting-preserving TOML parsing; unchanged files are rewritten | [#1490](https://github.com/databricks/unity-gateway/pull/1490), [#1502](https://github.com/databricks/unity-gateway/pull/1502) |
| Shared-state assembly repeatedly reads and hydrates saved state | [#1500](https://github.com/databricks/unity-gateway/pull/1500) |
| An alternate Claude config file causes repeated startup registration | [#1539](https://github.com/databricks/unity-gateway/pull/1539) |
| Skill refresh refetches shared sources and downloads after metadata work | [#1427](https://github.com/databricks/unity-gateway/pull/1427), [#1514](https://github.com/databricks/unity-gateway/pull/1514) |
| MCP changes discover OAuth metadata even when no registration can use it | [#1540](https://github.com/databricks/unity-gateway/pull/1540) |
| Typer reevaluates callback annotations; optional distribution/catalog imports remain eager | [#1491](https://github.com/databricks/unity-gateway/pull/1491), [#1499](https://github.com/databricks/unity-gateway/pull/1499) |
| The per-prompt orchestrator imports the command graph; subagents force token refreshes | [#1383](https://github.com/databricks/unity-gateway/pull/1383), [#1388](https://github.com/databricks/unity-gateway/pull/1388) |
| Codex service changes issue duplicate systemd reloads | [#1501](https://github.com/databricks/unity-gateway/pull/1501) |

Another current-main finding is duplicate preparation for Gemini, OpenCode, Kilo,
and Pi: CLI orchestration configures the agent, then its launch implementation
configures it again. [#1574](https://github.com/databricks/unity-gateway/pull/1574)
lets the adapter prepare once with the resolved model/provider and fresh credentials
after skill refresh. It preserves standalone calls and Gemini's periodic refresh,
and prevents Kilo's second write from overwriting the managed model choice.

## Profile discovery measurement

The real Databricks CLI v1.17.0 was run with four temporary PAT profiles, inert
credentials, and a loopback HTTP responder delayed by 150 ms per response. Five
measured samples alternated the two commands after one warmup each:

| Enumeration | Median | Validation requests per sample |
| --- | ---: | ---: |
| `auth profiles --output json` | 186.21 ms | 4 |
| `auth profiles --skip-validate --output json` | 29.15 ms | 0 |

The CLI validates profiles concurrently, so the delay depends on the slowest
unrelated profile rather than the sum of all profile requests. This controlled
component result is not a live workspace or end-to-end launch measurement.
Reproduce with the checked-in harness; its JSON includes raw samples, request
counts, CLI version, and observed metadata behavior:

```sh
uv run scripts/benchmark_profile_discovery.py --runs 5 --profiles 4 --delay-ms 150
```

Skipping validation preserves profile names, hosts, explicit auth types, and
workspace IDs in the measured CLI. It does omit the inferred auth type of a PAT
profile that only declares a token. UG therefore restores that classification
from the local config, without inheriting a DEFAULT profile's credentials or
overriding an explicit auth type. The normal selected-workspace authentication
check still runs, and debug-only full-profile diagnostics retain validation.

## Changes already on main

Several earlier improvements are already on the reference main:

- [#1291](https://github.com/databricks/unity-gateway/pull/1291) reuses successful
  Databricks executable discovery within a process.
- [#1372](https://github.com/databricks/unity-gateway/pull/1372) overlaps UC model
  discovery with gateway probing. Probe failure can abandon the background work.
- [#1472](https://github.com/databricks/unity-gateway/pull/1472) wakes the spinner
  immediately when work completes; its former 100 ms frame sleep no longer adds
  a completion delay.
- [#1475](https://github.com/databricks/unity-gateway/pull/1475) skips the Claude
  startup lock when the hook gate is already enabled.
- [#1474](https://github.com/databricks/unity-gateway/pull/1474) avoids repeating
  an unsuccessful or empty budget recommendation during the same launch.
- [#1265](https://github.com/databricks/unity-gateway/pull/1265) keeps ordinary
  launches from refreshing machine policy on every invocation. An initial
  uncached workspace can still require a control-plane read.

## Slow-path budgets

These are code-level contributions on the reference main, not measured user
latencies. They describe individual calls or loops and must not be added into
one claimed maximum: some overlap, some are mutually exclusive, and many run
only on failure, refresh, or first-time setup.

| Operation | Configured wait |
| --- | --- |
| Host-type resolution for workspace selection | 3 seconds; successful answers cached within the process |
| `databricks --version` discovery | 10 seconds per distinct executable, sequential on the reference main |
| Agent version discovery | 2 seconds per binary/timeout key, cached within the process |
| Profile enumeration | 20 seconds per subprocess; reference main also asks the CLI to validate profiles |
| `databricks auth token` | 15 seconds per attempt; four attempts only for recognized cache-lock contention |
| Noninteractive reauthentication | 30 seconds, followed by another token fetch when login succeeds |
| Browser-login helper / explicit login | 150-second browser-login budget / 300-second login subprocess timeout |
| Initial managed-policy GET | 30 seconds |
| UC model discovery | Four 30-second attempts per page, up to 100 pages; no aggregate deadline |
| Gateway probe | Up to three 10-second attempts plus backoff per page, at most 20 pages |
| Provider list/get | 30 seconds per request; listings can paginate |
| Foundation-model or Anthropic fallback | Default HTTP timeout of 10 seconds per request |
| Advisory budget recommendation | 30 seconds |
| Managed Codex catalog extraction and validation | Two subprocesses, each with a 30-second timeout |
| Codex smart-routing app-server health wait | 30-second loop budget; 1-second request timeout and 250 ms retry sleep |
| Dependency installation / agent upgrade | Separate installer subprocess budgets of 240–300 seconds |

`urllib` timeouts apply to socket operations, not a strict request-wide deadline.
DNS, multiple reads, pagination, interactive consent, and some service-manager
commands prevent these values from establishing a bounded end-to-end launch.
The gateway probe's retry backoff is normally about one then two seconds; a
server-supplied Retry-After can increase each delay to about five seconds.

## Import evidence and reproduction

A fresh-process `-X importtime` diagnostic of the reference main on Linux with
Python 3.12.3 reported 1,014 ms cumulative CLI import time in one sample.
Questionary accounted for 269 ms, including 244 ms in Prompt Toolkit. The agent
registry accounted for 165 ms, including 34 ms in smart-routing v2. These are
nested cumulative measurements: they overlap and must not be summed.
The trace did not load the Databricks SDK, MCP SDK, HTTPX, or Requests.
One trace provides attribution, not a median, percentile, or speedup claim.

Minor additional eager imports include Codex's macOS plist/Windows XML service
helpers and telemetry's platform-specific ctypes probes. They are candidates
for follow-up only after comparing their measured benefit with the existing
larger fixes; this audit claims no performance gain for deferring them.

Use one Python environment for both sources, outside the checkout whose imports
are being measured. For example, with a fresh disposable working directory:

```sh
cd /tmp
PYTHONPATH=/path/to/main/src /path/to/venv/bin/python \
  -X importtime -c 'import ucode.cli' 2> /tmp/ug-main-imports.txt
PYTHONPATH=/path/to/candidate/src /path/to/venv/bin/python \
  -X importtime -c 'import ucode.cli' 2> /tmp/ug-candidate-imports.txt
PYTHONPATH=/path/to/main/src /path/to/venv/bin/python -c \
  'import cProfile; cProfile.run("import ucode.cli", "/tmp/ug-main.prof")'
```

For timing comparisons, use fresh subprocesses, identical dependencies, explicit
environment isolation, bounded timeouts, warmups, and alternating source order.
Capture raw samples and interpreter/dependency/source versions. Include an
unchanged control such as `ug --version` to expose host-load variation. The
command-comparison harness is included in
[#1491](https://github.com/databricks/unity-gateway/pull/1491); phase-level launch
instrumentation is proposed in
[#1292](https://github.com/databricks/unity-gateway/pull/1292).

## Validation limits

This audit did not run live workspace launches, native prompt-readiness checks,
first-prompt inference, interactive login/install journeys, reboot/cold-disk
benchmarks, or production-cohort measurements. Import/help measurements exclude
remote authentication, gateway I/O, and native-agent initialization. A successful
process handoff alone does not establish a ready agent or successful task.

Older local reports compared combinations of pending changes on a busy host.
Their wall-time improvements cannot establish the independent benefit of each
PR. Focused regression checks should assert removed calls or overlap through
events/barriers; CI should not be required to meet this host's timing numbers.
Live comparisons must retain workspace selection, model visibility, policy,
authentication recovery, and native catalog validation semantics.
