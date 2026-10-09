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
    agent: str, ug: str, model: str | None, step_dir: Path, instruction: str, resume: bool
) -> tuple[list[str], str | None]:
    """Return argv plus the stdin to send. Codex takes the prompt as an argument."""
    model_args = ["--model", model] if model else []
    if agent == "claude":
        argv = [ug, "claude", *model_args, "--", "-p", "--verbose", "--output-format"]
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


def run_task(args: argparse.Namespace, task_dir: Path, out: Path) -> dict:
    config = tomllib.loads((task_dir / "task.toml").read_text())
    verifier_timeout = config.get("verifier", {}).get("timeout_sec", 120)
    workdir = out / "work" / task_dir.name
    logs = out / "logs" / task_dir.name
    logs.mkdir(parents=True, exist_ok=True)
    prepare(task_dir, workdir)

    env = {**os.environ, "TASK_APP_DIR": str(workdir), "IS_SANDBOX": "1"}
    if args.agent == "oracle":
        # Checks that read the agent transcript have nothing to read for the oracle.
        env["TASK_ORACLE"] = "1"
    # Tasks say "run it with Python", so put this interpreter first on PATH.
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    started = time.monotonic()
    result = {"task": task_dir.name, "passed": True, "agent_exit": None, "timed_out": False}
    for index, (name, step_dir, agent_timeout) in enumerate(task_steps(task_dir, config)):
        if args.agent != "oracle":
            env = bench_auth.agent_env(env)
        log_path = logs / f"{name}.log"
        env["TASK_AGENT_LOG"] = str(log_path)
        instruction = (step_dir / "instruction.md").read_text()
        argv, stdin = agent_command(
            args.agent, args.ug, args.model, step_dir, instruction, resume=index > 0
        )
        proc, timed_out = run_agent(argv, stdin, workdir, env, log_path, agent_timeout)
        # Verify before cleanup so a server the agent left running still counts against it.
        passed, verifier_log = verify(step_dir, workdir, env, verifier_timeout)
        (logs / f"{name}.verifier.log").write_text(verifier_log, encoding="utf-8")
        if not timed_out and os.name != "nt":
            kill_tree(proc)
        result.update(agent_exit=proc.returncode, timed_out=timed_out)
        if not passed:
            result.update(passed=False, failed_step=name)
            break
    result["duration_sec"] = round(time.monotonic() - started, 1)
    return result


def write_summary(args: argparse.Namespace, results: list[dict], out: Path) -> None:
    (out / "results.json").write_text(json.dumps(results, indent=2))
    passed = sum(r["passed"] for r in results)
    lines = [
        f"### Native tasks · {args.agent} · {sys.platform}: {passed}/{len(results)} passed",
        "",
        "| Task | Result | Agent exit | Time (s) |",
        "|------|--------|-----------|----------|",
    ]
    for r in results:
        status = "pass" if r["passed"] else ("timeout" if r["timed_out"] else "fail")
        lines.append(f"| {r['task']} | {status} | {r['agent_exit']} | {r['duration_sec']} |")
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

    results = [run_task(args, TASKS_DIR / name, out) for name in names]
    write_summary(args, results, out)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
