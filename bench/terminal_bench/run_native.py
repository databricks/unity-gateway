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

from ucode.os_compatibility.subprocess_cross_os import popen, run

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def agent_command(agent: str, ug: str, model: str | None, task_dir: Path) -> list[str]:
    model_args = ["--model", model] if model else []
    if agent == "claude":
        return [
            ug,
            "claude",
            *model_args,
            "--",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
    if agent == "codex":
        return [
            ug,
            "codex",
            *model_args,
            "--",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "-",
        ]
    return [sys.executable, str(task_dir / "solution" / "solve.py")]


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


def run_task(args: argparse.Namespace, task_dir: Path, out: Path) -> dict:
    config = tomllib.loads((task_dir / "task.toml").read_text())
    agent_timeout = config.get("agent", {}).get("timeout_sec", 600)
    verifier_timeout = config.get("verifier", {}).get("timeout_sec", 120)
    workdir = out / "work" / task_dir.name
    logs = out / "logs" / task_dir.name
    logs.mkdir(parents=True, exist_ok=True)
    prepare(task_dir, workdir)

    env = {**os.environ, "TASK_APP_DIR": str(workdir), "IS_SANDBOX": "1"}
    if args.agent != "oracle":
        env = bench_auth.agent_env(env)
    # Tasks say "run it with Python", so put this interpreter first on PATH.
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    instruction = (task_dir / "instruction.md").read_text()
    started = time.monotonic()
    timed_out = False
    with (logs / "agent.log").open("w", encoding="utf-8") as log:
        proc = popen(
            agent_command(args.agent, args.ug, args.model, task_dir),
            cwd=workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            proc.communicate(instruction, timeout=agent_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
    duration = time.monotonic() - started

    # Verify before cleanup so a server the agent left running still counts against it.
    try:
        verifier = run(
            [sys.executable, str(task_dir / "tests" / "test_outputs.py")],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=verifier_timeout,
        )
        passed = verifier.returncode == 0
        verifier_log = verifier.stdout + verifier.stderr
    except subprocess.TimeoutExpired:
        passed, verifier_log = False, f"verifier timed out after {verifier_timeout}s\n"
    (logs / "verifier.log").write_text(verifier_log, encoding="utf-8")
    if not timed_out and os.name != "nt":
        kill_tree(proc)
    return {
        "task": task_dir.name,
        "passed": passed,
        "agent_exit": proc.returncode,
        "timed_out": timed_out,
        "duration_sec": round(duration, 1),
    }


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

    names = args.task or sorted(p.name for p in TASKS_DIR.iterdir() if p.is_dir())
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
