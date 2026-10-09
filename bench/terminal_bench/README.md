# Terminal Bench for ug

This directory runs Terminal Bench tasks through `ug claude` and `ug codex`. It checks that an agent keeps working when ug configures and launches it. The `Terminal Bench` workflow in `.github/workflows/terminal-bench.yml` runs every night at 07:00 UTC and on manual dispatch.

The workflow has two lanes. The Harbor lane runs on Linux. It runs a subset of [Terminal Bench 2](https://www.tbench.ai/) and the tasks in `tasks/` inside Harbor's Docker containers, and `ug_agent.py` installs ug in each container. The native lane runs on Linux and Windows. GitHub's Windows runners can't run Linux containers, so `run_native.py` runs the tasks in `tasks/` directly on the runner.

```mermaid
flowchart LR
	workflow["Terminal Bench workflow"] --> harbor["Harbor lane<br/>Linux, Docker"]
	workflow --> native["Native lane<br/>Linux and Windows"]
	harbor --> tb2["TB2 subset"]
	harbor --> own["tasks/"]
	native --> own
```

Each lane runs every task for Claude and for Codex. [TASKS.md](TASKS.md) describes the task format and lists every task.

## How a task runs

Both lanes run a task the same way. The host mints the gateway token, so the agent never sees the service principal secret. The verifier runs after the agent exits and decides the result.

```mermaid
sequenceDiagram
	participant host as Runner host
	participant env as Task directory
	participant agent as ug and agent CLI
	participant gateway as AI Gateway
	host->>env: Copy environment/app/ and run environment/setup.py
	host->>host: bench_auth.py mints a token
	host->>agent: ug configure, then ug claude or ug codex with instruction.md
	agent->>gateway: Model calls with the token
	agent->>env: Read files, run commands, write output
	host->>env: Run tests/test_outputs.py
	env-->>host: Exit code 0 is a pass
```

The Harbor lane runs `ug configure` in each container. The native lane runs it once at the start of the job. A multi-step task mints a new token, launches the agent, and runs the verifier again for each step, resuming the agent's session each time.

## Run the workflow with other tasks or a model

To change the TB2 tasks or pin a model, dispatch the workflow with inputs:

```sh
gh workflow run terminal-bench.yml -f tb2_tasks=fix-git,code-from-image -f model=<model>
```

The workflow also takes `tb2_dataset`, `claude_version`, and `codex_version`. GitHub can dispatch the workflow only from a file that is already on `main`.

## Read the results

Each job writes a table of task results to its run summary.

A Harbor job fails only when a trial hits a harness error, such as a failed agent install or an API error. A task with reward 0 doesn't fail the job, because TB2 tasks are meant to be hard. A native job fails when any task fails, so a native job is often red while the Harbor jobs in the same run pass.

Each job uploads its files as an artifact named `terminal-bench-harbor-<agent>` or `terminal-bench-native-<agent>-<os>`. `scrub_secrets.py` replaces bearer tokens in those files before upload.

To tell a ug problem from an agent mistake, read the verifier output first and the agent log second:

- In a Harbor artifact, read `<job>/<trial>/verifier/test-stdout.txt`, then `<job>/<trial>/agent/ug-<agent>.txt`.
- In a native artifact, read `logs/<task>/<step>.verifier.log`, then `logs/<task>/<step>.log`. A single-step task names its step `agent`.

## Run tasks locally

To check the tasks without a gateway, run the reference solutions. Every task passes.

```sh
uv run python bench/terminal_bench/run_native.py --agent oracle --output /tmp/tb
```

Add `--task <name>` to run one task.

To run the tasks through ug, export credentials as described in [Credentials](#credentials) and pick an agent. The runner calls `ug configure` for that agent, so it replaces your own configuration for it.

```sh
uv run python bench/terminal_bench/run_native.py --agent claude --output /tmp/tb
```

To run in Harbor, you need Docker. Build a ug wheel, point `UG_BENCH_WHEEL` at it, and start a job:

```sh
uv build --wheel --out-dir /tmp/dist
export UG_BENCH_WHEEL=$(ls /tmp/dist/*.whl)
PYTHONPATH=bench/terminal_bench uvx --python 3.12 --from harbor==0.23.0 harbor run -y \
	-d terminal-bench@2.0 -i fix-git -a ug_agent:UgClaude -o /tmp/harbor-jobs
```

For Codex, use `ug_agent:UgCodex`. To run ug's own tasks, replace `-d terminal-bench@2.0` with `-p bench/terminal_bench/tasks --resume-trajectory`.

## Credentials

`bench_auth.py` picks the credentials for each task. If `UG_BENCH_CLIENT_ID`, `UG_BENCH_CLIENT_SECRET`, and `UG_BENCH_SP_WORKSPACE` are set, it mints a new OAuth M2M token on the host before each task. The agent gets that token and never the service principal secret, because the agent has a full shell. If those variables aren't set, it uses `DATABRICKS_BEARER` against `UCODE_TEST_WORKSPACE`.

CI uses the `UG_CUJ_SP_CLIENT_ID` service principal against `UG_CUJ1_WORKSPACE`. That workspace publishes a managed coding-agent config, so its model policy applies to every CI run. The policy doesn't allow haiku, for example.

## Add a task

1. Copy the task in `tasks/` that is closest to yours. [TASKS.md](TASKS.md) describes each file.
2. Write `tests/test_outputs.py` so it checks evidence that only the feature can produce, such as a hook log or an MCP call log, as well as the answer. An agent can read every file in the task, so the answer alone is easy to fake.
3. If only one agent supports the feature, set `agents` under `[metadata]` in `task.toml`.
4. Run `run_native.py --agent oracle --task <name>` and confirm the task passes.
5. Confirm the verifier fails when the agent does nothing. Copy `environment/app/` into an empty directory, or run `environment/setup.py <dir>`. Then run `tests/test_outputs.py` with `TASK_APP_DIR` set to that directory.
