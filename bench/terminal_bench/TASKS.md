# Task reference

Each directory in `tasks/` is one task in Harbor's task format. Harbor and `run_native.py` read the same files. `bench_tasks.py <agent>` prints the tasks that one agent runs.

## Files in a task

| Path | Contents |
|------|----------|
| `task.toml` | Timeouts, resources, and metadata. `agents` under `[metadata]` lists the agents that run the task, and defaults to Claude and Codex. Each `[[steps]]` entry adds a step. |
| `instruction.md` | The prompt the agent gets. |
| `environment/Dockerfile` | The Harbor image. It copies `environment/app/` to `/app` and runs `environment/setup.py /app` if the task has them. |
| `environment/app/` | Fixture files. The runner copies them into the task directory. |
| `environment/setup.py` | Generates fixtures. It takes the task directory as its only argument. |
| `tests/test_outputs.py` | The verifier. Exit code 0 is a pass. |
| `tests/test.sh` | Runs the verifier in Harbor and writes the reward to `/logs/verifier/reward.txt`. |
| `solution/solve.py` | The reference solution that `--agent oracle` runs. |
| `solution/solve.sh` | Runs the reference solution in Harbor. |

A multi-step task has `instruction.md`, `tests/`, and `solution/` under `steps/<name>/` for each step. Each step after the first resumes the previous session. Harbor resumes with `--resume-trajectory`. `run_native.py` passes `--continue` to Claude and `exec resume --last` to Codex. A failed step stops the task.

Verifiers and solutions use only the Python standard library. They run on Windows and in `python:3.12-slim`.

## Environment variables

Verifiers and solutions read these variables.

| Variable | Value |
|----------|-------|
| `TASK_APP_DIR` | The task directory. Defaults to `/app`, which is the task directory in Harbor. |
| `TASK_AGENT_LOG` | The agent's log for the current step. Only `run_native.py` sets it. In Harbor, read `/logs/agent/ug-<agent>.txt`. |
| `TASK_ORACLE` | `1` when the oracle runs. A check that reads the agent log skips itself. |

## ug's tasks

| Task | Agents | Passes when |
|------|--------|-------------|
| `user-hooks` | Claude | `release.txt` holds the codename from the project's `UserPromptSubmit` hook, and the `PostToolUse` hook logged the write. |
| `mcp-server` | Claude | `stock.json` matches the random values that the project's stdio MCP server logged for each lookup. |
| `project-instructions` | Claude, Codex | The output is under `out/`, and each new Python file starts with the license header that `CLAUDE.md` and `AGENTS.md` require. |
| `project-skill` | Claude | The release notes follow the project skill, and the skill's script logged a run. |
| `subagents` | Claude | Each file has a correct audit, and the transcript shows at least one subagent per file. |
| `image-code` | Claude, Codex | `code.txt` holds the 8-digit code drawn in `code.png`, with at least 6 digits right. |
| `pdf-extract` | Claude, Codex | `total.txt` holds the total from `invoice.pdf`, whose text stream is compressed. |
| `web-fetch` | Claude, Codex | `answer.txt` holds a value from a file in this repo, pinned to a fixed commit. |
| `background-shell` | Claude, Codex | `result.txt` holds the value that a job prints when it finishes. The job waits for the agent to ack a token it prints at start, so the agent must read its output while it runs. Claude must start it with `run_in_background`, and runs on `system.ai.claude-opus-4-8` instead of Smart Router's pick. |
| `resume-session` | Claude, Codex | Step 1 deletes `note.txt` without saving its code word to a file. Step 2 resumes the session and writes the code word to `answer.txt`. |
| `log-triage` | Claude, Codex | `report.json` has the correct counts for five independent service directories. |
| `fix-failing-tests` | Claude, Codex | The unit tests and extra hidden checks pass, and `tests/` is unchanged. |
| `scrub-git-secret` | Claude, Codex | Every copy of the token in history is replaced with `REDACTED`, and both branches keep their commit messages in order. |
| `background-service` | Claude, Codex | `token.txt` matches the token the server served, and nothing listens on the server's port afterward. |

## Default TB2 subset

The Harbor lane runs these Terminal Bench 2 tasks unless the `tb2_tasks` input overrides them: `fix-git`, `financial-document-processor`, `code-from-image`, `chess-best-move`, `git-multibranch`, `kv-store-grpc`, and `large-scale-text-editing`.
