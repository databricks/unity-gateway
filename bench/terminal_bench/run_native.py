"""Run Terminal Bench-format tasks directly on the host through ug.

Windows runners can't run the Linux containers Harbor uses, so this runner
copies each task's fixtures into a temp dir, launches the agent headlessly
there, and runs the task's Python verifier against the result.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import bench_auth
from bench_tasks import TASKS_DIR, names_for

from ucode.os_compatibility.subprocess_cross_os import popen, run


def agent_command(
    agent: str,
    ug: str,
    model: str | None,
    step_dir: Path,
    instruction: str,
    resume: bool,
    claude_task_model: str | None = None,
) -> tuple[list[str], str | None]:
    """Return argv plus the stdin to send. Codex takes the prompt as an argument."""
    model_args = ["--model", model] if model else []
    if agent == "claude":
        task_model = ["--model", claude_task_model] if claude_task_model and not model else []
        argv = [ug, "claude", *model_args, "--", *task_model, "-p", "--verbose", "--output-format"]
        argv += ["stream-json", "--dangerously-skip-permissions"]
        return argv + (["--continue"] if resume else []), instruction
    if agent == "codex":
        argv = [ug, "codex", *model_args, "--", "exec", *(["resume", "--last"] if resume else [])]
        argv += ["--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json"]
        return argv + ["--enable", "unified_exec", "--", instruction], None
    return [sys.executable, str(step_dir / "solution" / "solve.py")], None


def task_steps(task_dir: Path, config: dict) -> list[tuple[str, Path, float]]:
    """(name, dir holding instruction.md/tests/solution, agent timeout) for each step."""
    default = config.get("agent", {}).get("timeout_sec", 600)
    if not config.get("steps"):
        return [("agent", task_dir, default)]
    return [
        (
            step["name"],
            task_dir / "steps" / step["name"],
            step.get("agent", {}).get("timeout_sec", default),
        )
        for step in config["steps"]
    ]


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def prepare(task_dir: Path, workdir: Path) -> None:
    app = task_dir / "environment" / "app"
    if app.is_dir():
        shutil.copytree(app, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    setup = task_dir / "environment" / "setup.py"
    if setup.exists():
        run([sys.executable, str(setup), str(workdir)], check=True)


def run_agent(
    argv: list[str], stdin: str | None, workdir: Path, env: dict, log_path: Path, timeout: float
) -> tuple[subprocess.Popen, bool]:
    with log_path.open("w", encoding="utf-8") as log:
        proc = popen(
            argv,
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            return proc, True
    return proc, False


def verify(step_dir: Path, workdir: Path, env: dict, timeout: float) -> tuple[bool, str]:
    try:
        verifier = run(
            [sys.executable, str(step_dir / "tests" / "test_outputs.py")],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"verifier timed out after {timeout}s\n"
    return verifier.returncode == 0, verifier.stdout + verifier.stderr


def run_task(args: argparse.Namespace, task_dir: Path, out: Path, attempt: int) -> dict:
    config = tomllib.loads((task_dir / "task.toml").read_text())
    verifier_timeout = config.get("verifier", {}).get("timeout_sec", 120)
    workdir = out / "work" / task_dir.name / str(attempt)
    logs = out / "logs" / task_dir.name / str(attempt)
    logs.mkdir(parents=True, exist_ok=True)
    prepare(task_dir, workdir)

    env = {**os.environ, **config.get("environment", {}).get("env", {})}
    env.update(TASK_APP_DIR=str(workdir), IS_SANDBOX="1")
    env["ENABLE_SMART_ROUTING_V2"] = "1"
    if args.agent == "oracle":
        # Checks that read the agent transcript have nothing to read for the oracle.
        env["TASK_ORACLE"] = "1"
    # Tasks say "run it with Python", so put this interpreter first on PATH.
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    started = time.monotonic()
    result = {
        "task": task_dir.name,
        "attempt": attempt,
        "passed": True,
        "agent_exit": None,
        "timed_out": False,
    }
    for index, (name, step_dir, agent_timeout) in enumerate(task_steps(task_dir, config)):
        if args.agent != "oracle":
            env = bench_auth.agent_env(env)
        log_path = logs / f"{name}.log"
        env["TASK_AGENT_LOG"] = str(log_path)
        instruction = (step_dir / "instruction.md").read_text()
        argv, stdin = agent_command(
            args.agent,
            args.ug,
            args.model,
            step_dir,
            instruction,
            resume=index > 0,
            claude_task_model=env.get("UG_BENCH_CLAUDE_MODEL"),
        )
        proc, timed_out = run_agent(argv, stdin, workdir, env, log_path, agent_timeout)
        # Verify before cleanup so a server the agent left running still counts against it.
        passed, verifier_log = verify(step_dir, workdir, env, verifier_timeout)
        (logs / f"{name}.verifier.log").write_text(verifier_log, encoding="utf-8")
        # ug's routing wrappers log under ~/.ucode; keep them as evidence that routing ran.
        for ucode_log in (Path.home() / ".ucode").glob("*.log"):
            (logs / f"{name}.ucode").mkdir(exist_ok=True)
            shutil.copy2(ucode_log, logs / f"{name}.ucode" / ucode_log.name)
        if not timed_out and os.name != "nt":
            kill_tree(proc)
        result.update(agent_exit=proc.returncode, timed_out=timed_out)
        if not passed:
            result.update(passed=False, failed_step=name)
            break
    result["duration_sec"] = round(time.monotonic() - started, 1)
    return result


def failing_tasks(results: list[dict], min_passes: int) -> list[str]:
    passes: dict[str, int] = {}
    for r in results:
        passes[r["task"]] = passes.get(r["task"], 0) + r["passed"]
    return [task for task, n in passes.items() if n < min_passes]


def write_summary(args: argparse.Namespace, results: list[dict], out: Path) -> None:
    (out / "results.json").write_text(json.dumps(results, indent=2))
    by_task: dict[str, list[dict]] = {}
    for r in results:
        by_task.setdefault(r["task"], []).append(r)
    failing = failing_tasks(results, args.min_passes)
    lines = [
        f"### Native tasks · {args.agent} · {sys.platform}: "
        f"{len(by_task) - len(failing)}/{len(by_task)} tasks passed"
        f" (each needs {args.min_passes}/{args.attempts} passing attempts)",
        "",
        "| Task | Passed | Results | Agent exits | Time (s) |",
        "|------|--------|---------|-------------|----------|",
    ]
    for task, runs in by_task.items():
        statuses = [
            "pass" if r["passed"] else ("timeout" if r["timed_out"] else "fail") for r in runs
        ]
        lines.append(
            f"| {task} | {sum(r['passed'] for r in runs)}/{len(runs)} | {', '.join(statuses)} "
            f"| {', '.join(str(r['agent_exit']) for r in runs)} "
            f"| {', '.join(str(r['duration_sec']) for r in runs)} |"
        )
    summary = "\n".join(lines) + "\n"
    print(summary)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["claude", "codex", "oracle"], required=True)
    parser.add_argument("--task", action="append", help="Task name; repeatable. Default: all.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ug", default=shutil.which("ug"), help="Absolute path to ug.")
    parser.add_argument("--model")
    parser.add_argument("--attempts", type=int, default=3, help="Runs of each task.")
    parser.add_argument(
        "--min-passes",
        type=int,
        default=2,
        help="Passing attempts each task needs for exit code 0. Agents miss some runs.",
    )
    args = parser.parse_args()

    names = args.task or names_for(args.agent)
    out = args.output.resolve()
    if (out / "work").exists():
        shutil.rmtree(out / "work")
    out.mkdir(parents=True, exist_ok=True)

    if args.agent != "oracle":
        if not args.ug:
            parser.error("--ug is required when ug isn't on PATH")
        args.ug = str(Path(args.ug).resolve())
        env = bench_auth.agent_env(dict(os.environ))
        run(
            [
                args.ug,
                "configure",
                "--agents",
                args.agent,
                "--workspace",
                env["DATABRICKS_HOST"],
                "--skip-validate",
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ],
            check=True,
            env=env,
            stdin=subprocess.DEVNULL,
        )

    results = []
    for name in names:
        passes = fails = 0
        # Stop once the task has enough passes, or too many fails to get them.
        while passes < args.min_passes and fails <= args.attempts - args.min_passes:
            result = run_task(args, TASKS_DIR / name, out, passes + fails + 1)
            results.append(result)
            if result["passed"]:
                passes += 1
            else:
                fails += 1
    write_summary(args, results, out)
    return 0 if results and not failing_tasks(results, args.min_passes) else 1


if __name__ == "__main__":
    sys.exit(main())
