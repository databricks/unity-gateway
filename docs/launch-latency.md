# UG launch latency

Record UG launches locally and build an offline dashboard with p50, p90, launch
counts, trends, and breakdowns by agent and UG version. The dashboard filters by
UTC date, agent, version, platform, managed config, first-run setup, preflight mode,
and requested smart routing.

From this checkout, enable capture in the terminal where you launch agents:

```sh
export UG_LAUNCH_LATENCY_PATH="$HOME/.ucode/launch-latency.jsonl"
uv run ug claude
# Or launch another agent normally, for example: uv run ug codex
```

PowerShell:

```powershell
$env:UG_LAUNCH_LATENCY_PATH = "$HOME/.ucode/launch-latency.jsonl"
uv run ug claude
```

The installed `ug` command must include the launch instrumentation to record
observations. Use `uv run ug` in this checkout, or install the updated checkout
with `uv tool install --reinstall .` before using the installed command.

Generate the dashboard after collecting launches:

```sh
python scripts/launch_latency_dashboard.py \
  --input "$HOME/.ucode/launch-latency.jsonl" \
  --output /tmp/ug-launch-latency.html
```

Open the generated HTML file in a browser. It contains the records, styles,
charts, and filter logic, and works without a web server or network access.
Regenerate it to include new observations. A missing input file produces an
empty dashboard with setup instructions; no demonstration measurements are added.

Unset `UG_LAUNCH_LATENCY_PATH` to stop recording. Capture is opt-in; no telemetry
destination or fleetwide collection is configured. The local JSONL file can be
copied or merged for a wider cohort, provided every launch retains its event ID.
The dashboard keeps the latest valid row for each event ID and reports duplicate
and invalid row counts. Incomplete JSONL lines from an interrupted write are ignored.
Recording is best-effort: lock contention or an unwritable destination can drop
observations so launching does not wait for telemetry.

## What is measured

`duration_ms` measures from UG's Python entrypoint, before importing the CLI, to
the native agent process handoff boundary. It includes CLI imports, authentication,
configuration, model discovery, and launch preparation performed along that path.
It excludes Python interpreter initialization, an outer launcher such as `uv`,
the agent's own initialization, prompt readiness, and the rest of the session.
A handoff observation proves only that UG reached the launch boundary.
For Claude's PTY path, that boundary is the fork: the child performs no timing
I/O before exec, so a later child exec failure does not change the handoff record.

Failures after agent selection but before handoff are counted separately and
excluded from handoff latency percentiles. Failures before agent selection and
forcefully terminated processes may produce no observation. Forwarded native
agent subcommands also count as handoffs; these counts do not identify successful
interactive sessions.

Interactive authentication, installation, configuration, and first-run setup can
increase measured time. Use **First-run setup → Already configured** to exclude
launches that triggered automatic configuration. Compare similar launch cohorts
with the managed, preflight, and routing filters too. `smart_routing` reflects the
requested launch mode; routing setup can fall back to a normal launch before
handoff. These records do not independently identify every interactive step.

## p50 and p90

p50 is the median launch duration; p90 describes the slower end of the observed
launches. The dashboard computes each percentile from raw handoff durations in
the selected cohort. It sorts the samples and linearly interpolates at index
`(n - 1) × q`, where `q` is `0.5` or `0.9`. For example, durations of 1, 2, 3,
and 4 seconds give p50 = 2.5 seconds and p90 = 3.7 seconds.

The headline values use all selected handoff samples. Trend points calculate
percentiles independently for each UTC hour or day. The dashboard never averages
percentiles from different users, versions, or time buckets. Counts appear next
to the percentiles, and selections with fewer than 20 handoffs show a small-sample
notice. A single observation has identical p50 and p90; no observations show `—`.

## Records and privacy

Version-1 JSONL records contain a unique `event_id`, UTC `started_at`,
`duration_ms`, `outcome` (`handoff` or `failed`), and launch dimensions: `agent`,
`ug_version`, `os`, `arch`, `managed`, `auto_configure`, `skip_preflight`, `refresh`, and
`smart_routing`. Missing dimensions appear as unknown in the dashboard.

Capture does not include prompts, command arguments, tokens, custom headers, or
credential files. The generated HTML embeds the accepted measurement fields;
treat it as a copy of those local observations when sharing it. The input log
is append-only and has no size or retention limit. Rotate, archive, or remove it
manually when you no longer need those measurements.
