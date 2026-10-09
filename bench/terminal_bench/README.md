# Terminal Bench

Runs Terminal Bench-style tasks through `ug claude` and `ug codex`. The
`Terminal Bench` workflow runs nightly and on demand.

There are two lanes:

- Harbor (Linux). Runs real [Terminal Bench 2](https://www.tbench.ai/) tasks
  plus the tasks in `tasks/` inside Harbor's Docker containers.
  `ug_agent.py` installs the ug wheel, the Databricks CLI, and the agent CLI in
  each container, then launches the agent headlessly through ug.
- Native (Linux and Windows). GitHub's Windows runners can't run Linux
  containers, so `run_native.py` runs the tasks in `tasks/` on the host.

The default TB2 subset was picked to make the agent spawn subagents, use a range
of tools, and touch system state (services, global installs, git history
rewrites). Change it with the workflow's `tb2_tasks` input.

## Tasks

Each task follows the Harbor layout: `task.toml`, `instruction.md`,
`environment/` (a Dockerfile, plus `app/` fixtures or a `setup.py`),
`tests/test_outputs.py`, and `solution/solve.py`. The verifier and solution read
the task dir from `TASK_APP_DIR` (default `/app`) so the same files work in
both lanes. Keep them standard-library Python so they run on Windows too.

Most tasks check one agent feature that should keep working when the agent
runs through ug. Each one leaves evidence only that feature can produce, such as
a hook log, an MCP call log, or a subagent's model in the transcript. Tasks that
only one agent supports list it in `[metadata] agents`. `bench_tasks.py <agent>`
prints the tasks for an agent.

| Task | Exercises | Agents |
|------|-----------|--------|
| `user-hooks` | Project `UserPromptSubmit` and `PostToolUse` hooks still run | Claude |
| `mcp-server` | A project stdio MCP server is loaded and called | Claude |
| `project-instructions` | `CLAUDE.md` / `AGENTS.md` conventions are followed | Both |
| `subagent-model` | A custom subagent pinned to `model: haiku` runs on the gateway's haiku model | Claude |
| `image-code` | Reading an image through the gateway | Both |
| `web-fetch` | Fetching a pinned URL | Both |
| `resume-session` | Two steps: the second resumes the first session and recalls a value | Both |
| `log-triage` | Independent per-service work that can fan out to subagents | Both |
| `fix-failing-tests` | Run tests, edit, rerun | Both |
| `scrub-git-secret` | Destructive git history rewrite across branches | Both |
| `background-service` | Start a background process, query it, shut it down | Both |

Multi-step tasks put `instruction.md`, `tests/`, and `solution/` under
`steps/<name>/`. Harbor runs them with `--resume-trajectory`, and `run_native.py`
resumes with `--continue` or `exec resume --last`.

## Running locally

Check tasks with the reference solutions (no gateway needed):

```sh
uv run python bench/terminal_bench/run_native.py --agent oracle --output /tmp/tb
```

## Auth

`bench_auth.py` picks the credentials. With `UG_BENCH_CLIENT_ID`,
`UG_BENCH_CLIENT_SECRET`, and `UG_BENCH_SP_WORKSPACE` set, it mints a fresh
OAuth M2M token on the host before each task. Only that token reaches the agent,
never the SP secret. Otherwise it uses `DATABRICKS_BEARER` against
`UCODE_TEST_WORKSPACE`. CI uses the CUJ service principal and `UG_CUJ1_WORKSPACE`.

Uploaded artifacts have tokens redacted by `scrub_secrets.py`.

Run the tasks through ug with either set of credentials exported. This rewrites
your `ug configure` state for the agent:

```sh
uv run python bench/terminal_bench/run_native.py --agent claude --output /tmp/tb
```

Harbor needs Docker, `UG_BENCH_WHEEL`, and the same credentials:

```sh
uv build --wheel --out-dir /tmp/dist && export UG_BENCH_WHEEL=$(ls /tmp/dist/*.whl)
PYTHONPATH=bench/terminal_bench uvx --python 3.12 --from harbor==0.23.0 harbor run -y \
  -d terminal-bench@2.0 -i fix-git -a ug_agent:UgClaude -o /tmp/harbor-jobs
```
