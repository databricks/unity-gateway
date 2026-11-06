# Offline startup profiling

Use `scripts/profile_startup.py` to measure fresh-process startup and identify
Python import costs without signing in, launching an agent, or changing user
configuration. It is a measurement tool; runtime optimizations land separately.

```sh
uv run --frozen scripts/profile_startup.py \
  --runs 15 --warmups 2 \
  --json /tmp/ug-startup.json \
  --profiles /tmp/ug-startup-profiles
```

Each process has an isolated home and no inherited UG, agent, or authentication
settings. The helper cases use an inert bearer and `https://benchmark.invalid`,
exercising the real parser and external-bearer path without network calls. Their
captured output is checked without printing it. No native agent is executed.

The script covers interpreter startup, version output, CLI import, top-level help,
Claude/MCP help, proxy help, and credential-helper startup/help. Select cases with
repeated `--case` options:

```sh
uv run --frozen scripts/profile_startup.py \
  --case auth-token --case auth-full-cli --runs 15
```

`auth-token` enters through the console-script entrypoint. `auth-full-cli` invokes
the same command through `ucode.cli`. On revisions without a dedicated helper
dispatch, these cases take equivalent paths. On revisions containing the
[helper fast path](https://github.com/databricks/unity-gateway/pull/1384), they
isolate the cost of loading the full CLI. This profiling change does not install
that optimization.

## Comparing revisions

Keep dependencies and the Python interpreter identical when comparing source
trees. `--source` and `--baseline-source` accept checkout roots:

```sh
uv run --frozen scripts/profile_startup.py \
  --source /path/to/candidate \
  --baseline-source /path/to/baseline \
  --runs 30 --warmups 2 \
  --json /tmp/ug-comparison.json \
  --profiles /tmp/ug-comparison-profiles
```

Source and case order alternate each round to reduce bias from filesystem caches
and changing machine load. Warmups warm filesystem/bytecode caches; processes
never share Python imports. `--python` selects another interpreter with UG's
dependencies installed. Both sources use that same interpreter and dependency set.

JSON records raw samples, median, minimum, nearest-rank p95, source paths, command
shapes, interpreter, and platform. On platforms with `resource.getrusage`, it also
records each child's user-plus-system CPU time. CPU fields are null/empty on
Windows. Comparing interpreter/version/proxy controls and CPU time helps identify
scheduler noise; it does not remove that noise from wall-clock samples.

The `--profiles` directory contains one source directory per variant, with
`<case>.importtime` and `<case>.prof` files. These are separate diagnostic runs and
do not enter the latency samples. Read a CPU profile with:

```sh
python -m pstats /tmp/ug-startup-profiles/current/cli-import.prof
```

All commands have closed stdin and a bounded timeout. A failure or unexpected
credential-helper output fails the benchmark instead of becoming a timing sample.

## Measurement limits

These timings include Python initialization and process exit. They exclude an
outer launcher such as `uv`, remote authentication, gateway latency, native agent
initialization, and prompt readiness. Help/version results do not establish that
an agent can complete a task. Local percentile estimates are not fleet statistics;
small samples, host load, filesystem caches, and installed dependencies affect them.

During the November 6 investigation, 15 measured samples after two warmups compared
commit `12cec72d` with a combined local prototype on Python 3.12.3/Linux. The
prototype included deferred imports, helper dispatch, command annotation reuse,
discovery/configuration changes, and launch-timing instrumentation. It was not this
tools-only branch and its measurements do not establish any individual PR's gain.

| Case | Wall median before → prototype | CPU median before → prototype |
| --- | ---: | ---: |
| CLI import | 1,627 → 822 ms | 605 → 434 ms |
| Top-level help | 1,941 → 1,239 ms | 812 → 589 ms |
| Credential helper | 1,623 → 591 ms | 684 → 307 ms |

Host contention made wall-clock controls noisy; their CPU medians stayed within
3%. Re-run the comparison against the specific reviewed branches before making
release or production-latency claims. The controlled model-family benchmark is
maintained with the [discovery-concurrency PR](https://github.com/databricks/unity-gateway/pull/1476).
