# Collect a Claude subagent routing snapshot

Use this when Isaac reports an error such as:

```text
Agent type 'ucode-route-kimi-k3-ba377e31' not found. Available agents: ...
```

Keep the affected session open and run the script from that session's shell tool:

```bash
python3 /path/to/diagnose_claude_routing.py
```

The script needs Python 3.12+ on Linux or macOS and has no package dependencies.
It selects the nearest Claude ancestor. When run from another terminal, specify
the affected **Claude process** and session UUID:

```bash
python3 /path/to/diagnose_claude_routing.py \
  --pid 12345 \
  --session-id 00000000-0000-0000-0000-000000000000 \
  --output ./routing-report
```

Replace the example PID/UUID with real values. Without a Claude ancestor or
`--pid`, the report lists candidate processes but does not select an unrelated
session. If launch arguments do not identify one unambiguous session, it lists
recent session UUIDs for that project. Rerun with the affected UUID to include
session evidence. Use a new output directory on each run.

The output contains `summary.txt` and `report.json`. The directory is private
(0700) and the files are private (0600). Inspect them and share them with the
engineer investigating the issue. Nothing is uploaded automatically.

## Skill-ready instructions

Copy `diagnose_claude_routing.py` into the skill's `scripts/` directory and embed:

> When a routed subagent reports “Agent type ... not found,” keep the affected
> Isaac session open. Run `python3 <skill-directory>/scripts/diagnose_claude_routing.py`
> through that session's shell tool. Read `summary.txt`. If the script reports an
> ambiguous or unknown session, identify the affected session UUID and rerun
> with `--session-id UUID`. From a different terminal, also supply `--pid` for
> the affected Claude process. Return the report paths and summarize any
> mismatches or unavailable evidence. Ask the user to review the report before
> sharing it. Do not restart, reconfigure, or upgrade the session to collect
> this snapshot.

## What the evidence means

- `process.agents_arguments` lists names and models supplied via `--agents`.
  Older UG versions create transient definitions this way. New launches use
  `--plugin-dir`, recorded under `process.launch_options`. Neither requires
  registration in personal `agents/` directories or the installed-plugin registry.
  The collector does not resolve plugin agents; if plugin paths are supplied,
  registry comparisons are unknown rather than reporting absent `--agents` as a defect.
- `findings` compares each launch routing hook's model list with the supplied
  definitions. A match does **not** establish that Claude retained the
  definitions in memory. An actual tool error's `available_agents` list is
  separate evidence of what Claude reported at failure time.
  Process arguments describe the current process, not immutable exec history:
  a program can rewrite them. Absent flags therefore do not prove a direct
  launch. Likewise, PPID 1 can mean the original parent exited.
- Settings are inventoried by source, including file hashes and timestamps.
  This is not a reimplementation of Claude's full settings precedence.
- `hook_executables` can reveal missing or changed hook binaries. `executables`
  records PATH resolution and installed UG/Claude versions when discoverable.
  Isaac's build version is not inferred from its wrapper or obtained by
  launching it. If needed, separately attach `isaac version` output.
- `logs` includes routing decisions, subagent starts, spawn calls, and actual
  missing-agent tool errors for the selected session. Quoted errors in normal
  user/assistant text are excluded. Logs are bounded to their last 2 MiB;
  truncation and malformed lines are reported.
  A resumed session can contain errors from earlier process lifetimes; compare
  their timestamps with the selected process's start time.
- `subagents` records child metadata, child tool errors, and response model identifiers. Gateway
  deployment names in responses may differ from the public model ID; this
  alone is not a routing mismatch. Missing model information remains unknown.
- `global_canary` describes the last recorded SessionStart across all sessions;
  it is not used to identify the affected process.

For daemon-backed background sessions, collect the **worker serving the failed
session**, not just the daemon or an interactive frontend. If useful, take
separate snapshots of the worker and daemon using their explicit PIDs. A fresh
Isaac frontend does not establish that its worker or daemon is fresh. Environment
flags enable an installed routing hook; they do not install that hook by
themselves. Use the settings inventory and session evidence to establish its
source, and leave it unknown if the runtime passed settings through another
channel. Forked processes can retain memory and arguments; a new exec receives
the arguments its launcher supplies. Current argv alone cannot distinguish these
lifecycle paths.

Do not interpret `launch_hook_vs_supplied_agents: match` as proof of a healthy
runtime registry: a reload can discard CLI-supplied definitions while the process
retains its original arguments. This is distinct from a background worker that
never received definitions; do not assume the same trigger without session evidence.

The collector reads configuration, process metadata, and existing logs. It does
not invoke hooks, launch agents, refresh credentials, change configuration, or
install packages. It omits credentials, full environments, hook command bodies,
agent prompts, and conversation text. It retains model IDs, agent/plugin names,
session IDs, and local paths (with the current home directory replaced by
`$HOME`). Process inspection may be unavailable because of OS permissions, an
exited process, or a different process namespace. macOS uses native process
argument inspection; its adapter has fixture coverage but still needs a native
macOS smoke test.
