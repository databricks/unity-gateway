"""Measure `ug` command latency on two builds and render a before/after PR comment.

Used by `.github/workflows/ug-latency.yml` (`/ug-latency` PR comment). The workflow always runs
this file from the default branch; only the two `ug` builds under test come from the PR.

    ug_latency.py measure --before BIN --after BIN --workspace URL --output results.json
    ug_latency.py render results.json --output comment.md

Only `ug`'s own overhead is measured: `claude` and `codex` are replaced by stub executables that
exit immediately, so `ug claude` / `ug codex` time ends when `ug` has handed over to the agent.
Standard library only, so it runs under any Python 3.9+ without installing the project.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MARKER = "<!-- ug-latency-comment -->"
SCHEMA_VERSION = 1
DEFAULT_ITERATIONS = 5
MAX_ITERATIONS = 15
DEFAULT_WARMUP = 1
COMMAND_TIMEOUT_SECONDS = 300
# A delta is reported as a change only beyond both thresholds; smaller moves are run-to-run noise.
NOISE_PERCENT = 5.0
NOISE_SECONDS = 0.05
SUDO = "/usr/bin/sudo"
# Root-owned files `ug configure` (global scope) writes. Only removed when explicitly asked, so
# every timed global configure does the privileged write instead of finding it already done.
GLOBAL_PATHS = ("/etc/claude-code", "/etc/codex", "/etc/unitygateway")
AGENT_STUB_VERSION = "99.0.0"
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
CONFIGURE_BASE_ARGS = (
    "configure",
    "--agents",
    "claude,codex",
    "--disable-databricks-ai-tools",
)


@dataclass(frozen=True)
class Spec:
    id: str
    label: str
    kind: str  # "configure" (fresh HOME each run) or "launch" (configured HOME, reused)
    args: tuple[str, ...]  # appended after the base arguments
    needs_sudo: bool = False


SPECS: dict[str, Spec] = {
    "configure": Spec("configure", "ug configure", "configure", (), needs_sudo=True),
    "configure-user": Spec("configure-user", "ug configure --user", "configure", ("--user",)),
    "configure-global": Spec(
        "configure-global", "ug configure --global", "configure", ("--global",), needs_sudo=True
    ),
    "claude": Spec("claude", "ug claude", "launch", ("claude",)),
    "codex": Spec("codex", "ug codex", "launch", ("codex",)),
}
DISPLAY_ORDER = ("configure", "configure-user", "configure-global", "claude", "codex")
# Machine-wide writes go last so their leftovers never reach the user-scope and launch runs.
EXECUTION_ORDER = ("configure-user", "claude", "codex", "configure", "configure-global")


# ---------------------------------------------------------------------------------------------
# Pure logic (unit-tested)
# ---------------------------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..100) of a non-empty sample."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(samples: Sequence[float]) -> dict[str, float] | None:
    """Median, min, p90 and max of the successful runs, or None without any."""
    if not samples:
        return None
    return {
        "median": statistics.median(samples),
        "min": min(samples),
        "p90": percentile(samples, 90),
        "max": max(samples),
    }


def compare(before: Mapping[str, float] | None, after: Mapping[str, float] | None) -> dict | None:
    """Median delta (after - before) in seconds and percent, plus a verdict."""
    if not before or not after:
        return None
    delta = after["median"] - before["median"]
    percent = (delta / before["median"] * 100.0) if before["median"] > 0 else 0.0
    if abs(delta) < NOISE_SECONDS or abs(percent) < NOISE_PERCENT:
        verdict = "within noise"
    else:
        verdict = "slower" if delta > 0 else "faster"
    return {"delta": delta, "percent": percent, "verdict": verdict}


def parse_iterations(value: str | int) -> int:
    count = int(value)
    if not 1 <= count <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_ITERATIONS}")
    return count


def parse_command_list(value: str) -> list[str]:
    """Validate a comma-separated list of command ids; returned in display order."""
    requested = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in requested if item not in SPECS]
    if unknown or not requested:
        raise ValueError(
            f"unknown command id(s) {unknown or value!r}; choose from {', '.join(DISPLAY_ORDER)}"
        )
    return [name for name in DISPLAY_ORDER if name in requested]


def build_argv(binary: str, spec: Spec, workspace: str) -> list[str]:
    """The exact `ug` argv for one timed run. Launch specs get the configure argv separately."""
    if spec.kind == "launch":
        return [binary, *spec.args]
    return [binary, *CONFIGURE_BASE_ARGS, "--workspace", workspace, *spec.args]


def setup_argv(binary: str, workspace: str) -> list[str]:
    """Untimed configure that gives launch measurements their saved state (no sudo)."""
    return [binary, *CONFIGURE_BASE_ARGS, "--workspace", workspace, "--user"]


def run_order(iteration: int) -> tuple[str, str]:
    """Alternate which build runs first so drift and cache warmth do not favour one side."""
    return ("before", "after") if iteration % 2 == 0 else ("after", "before")


def isolated_environment(
    home: Path,
    stub_dir: Path,
    base_env: Mapping[str, str],
    bearer: str | None,
    bin_dir: Path | None = None,
) -> dict[str, str]:
    keep = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    )
    env = {key: base_env[key] for key in keep if key in base_env}
    # The build's own bin directory comes next, so the `ug` that `--global` finds on PATH is the
    # build under test and not another installation on the runner.
    front = [str(stub_dir)] + ([str(bin_dir)] if bin_dir else [])
    env["PATH"] = os.pathsep.join([*front, env.get("PATH", "")])
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CODEX_HOME": str(home / ".codex"),
            "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
            "DISABLE_AUTOUPDATER": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "160",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if bearer:
        env["DATABRICKS_BEARER"] = bearer
    return env


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _seconds(value: object) -> float | None:
    """A finite non-negative float, or None. Result files are untrusted when rendered."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.2f} s"


def _fmt_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return sign + _fmt(abs(value))


def _stat(side: object, key: str) -> float | None:
    if not isinstance(side, dict):
        return None
    summary = side.get("summary")
    return _seconds(summary.get(key)) if isinstance(summary, dict) else None


def _count(side: object, key: str) -> int:
    value = side.get(key) if isinstance(side, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_sha(value: object) -> str:
    return value[:12] if isinstance(value, str) and SHA_RE.fullmatch(value) else "unknown"


def render_markdown(data: Mapping, run_url: str | None = None) -> str:
    """The PR comment. Built only from validated numbers and known ids: the result file comes
    from a job that ran PR code, so no free text from it is ever copied into the comment."""
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    iterations = _count(meta, "iterations")
    warmup = _count(meta, "warmup")
    lines = [
        MARKER,
        "## `ug` command latency (before vs after)",
        "",
        f"Base `{_safe_sha(meta.get('base_sha'))}` (merge base) vs head "
        f"`{_safe_sha(meta.get('head_sha'))}`. {iterations} timed runs per command after "
        f"{warmup} warmup, build order alternated, `claude`/`codex` stubbed so only `ug` "
        "is measured. Times are wall-clock from process start to exit.",
        "",
        "| Command | Median before | Median after | Delta | Delta % | Min (b / a) | "
        "p90 (b / a) | Max (b / a) | OK runs (b / a) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    notes: list[str] = []
    results = data.get("results")
    by_id = (
        {r.get("id"): r for r in results if isinstance(r, dict)}
        if isinstance(results, list)
        else {}
    )
    for command_id in DISPLAY_ORDER:
        spec = SPECS[command_id]
        entry = by_id.get(command_id)
        if entry is None:
            continue
        before, after = entry.get("before"), entry.get("after")
        if entry.get("status") == "skipped":
            lines.append(f"| `{spec.label}` | skipped | skipped | | | | | | |")
            notes.append(f"`{spec.label}` was skipped (needs passwordless sudo on the runner).")
            continue
        change = compare(
            {"median": m} if (m := _stat(before, "median")) is not None else None,
            {"median": m} if (m := _stat(after, "median")) is not None else None,
        )
        if change is None:
            delta_cell = pct_cell = "n/a"
        else:
            delta_cell = _fmt_delta(change["delta"])
            sign = "+" if change["percent"] > 0 else ""
            pct_cell = f"{sign}{change['percent']:.1f}%"
            if change["verdict"] != "within noise":
                delta_cell = f"**{delta_cell}** ({change['verdict']})"

        def pair(key: str, before: object = before, after: object = after) -> str:
            return f"{_fmt(_stat(before, key))} / {_fmt(_stat(after, key))}"

        ok_b, ok_a = _count(before, "ok"), _count(after, "ok")
        bad_b, bad_a = _count(before, "failures"), _count(after, "failures")
        lines.append(
            f"| `{spec.label}` | {_fmt(_stat(before, 'median'))} | {_fmt(_stat(after, 'median'))} "
            f"| {delta_cell} | {pct_cell} | {pair('min')} | {pair('p90')} | {pair('max')} "
            f"| {ok_b} / {ok_a} |"
        )
        setup_bad = [
            name
            for name, side in (("before", before), ("after", after))
            if isinstance(side, dict) and side.get("setup_failed") is True
        ]
        if setup_bad:
            notes.append(
                f"`{spec.label}`: the untimed setup `ug configure --user` failed on "
                f"{' and '.join(setup_bad)}, so no runs were timed there (see the workflow log)."
            )
        elif bad_b or bad_a:
            notes.append(
                f"`{spec.label}`: {bad_b} failed run(s) before, {bad_a} after (not timed; "
                "see the workflow log)."
            )
    lines += [
        "",
        f"A delta is marked only when it exceeds both {NOISE_PERCENT:g}% and "
        f"{NOISE_SECONDS * 1000:.0f} ms; otherwise it is within run-to-run noise.",
    ]
    if notes:
        lines += ["", *[f"- {note}" for note in notes]]
    if run_url and re.fullmatch(r"https://[\w./:-]+", run_url):
        lines += ["", f"[Workflow run]({run_url})"]
    return "\n".join(lines) + "\n"


def render_failure(run_url: str | None) -> str:
    """Comment body when the measurement job itself failed."""
    link = f" See the [workflow run]({run_url})." if run_url else ""
    return f"{MARKER}\n## `ug` command latency (before vs after)\n\nThe measurement failed.{link}\n"


# ---------------------------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------------------------


def write_stubs(directory: Path) -> None:
    """`claude` and `codex` that report a new-enough version and exit 0 at once."""
    directory.mkdir(parents=True, exist_ok=True)
    scripts = {
        "claude": f'#!/bin/sh\ncase "$1" in --version) echo "{AGENT_STUB_VERSION} (Claude Code)";; esac\nexit 0\n',
        "codex": f'#!/bin/sh\ncase "$1" in --version) echo "codex-cli {AGENT_STUB_VERSION}";; esac\nexit 0\n',
    }
    for name, body in scripts.items():
        path = directory / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)


def passwordless_sudo() -> bool:
    if not os.path.exists(SUDO):
        return False
    return (
        subprocess.run(  # noqa: TID251 (standalone CI script, not part of the package)
            [SUDO, "-n", "true"], capture_output=True, check=False
        ).returncode
        == 0
    )


def reset_global_state() -> None:
    subprocess.run(  # noqa: TID251
        [SUDO, "-n", "rm", "-rf", "--", *GLOBAL_PATHS], capture_output=True, check=False
    )


def timed_run(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> tuple[float, int, str]:
    start = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: TID251
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return time.perf_counter() - start, 124, f"timed out after {COMMAND_TIMEOUT_SECONDS}s"
    elapsed = time.perf_counter() - start
    return elapsed, proc.returncode, (proc.stderr or proc.stdout or "")[-1500:]


def measure(
    *,
    binaries: Mapping[str, str],
    workspace: str,
    commands: Sequence[str],
    iterations: int,
    warmup: int,
    work_dir: Path,
    bearer: str | None,
    reset_global: bool,
    meta: Mapping[str, object] | None = None,
    checkpoint: Callable[[dict], None] | None = None,
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr, flush=True),
) -> dict:
    """Time every command; `checkpoint` receives the payload so far after each finished command."""
    stub_dir = work_dir / "stubs"
    write_stubs(stub_dir)
    secrets = [bearer or "", workspace]
    wants_sudo = reset_global or any(SPECS[c].needs_sudo for c in commands)
    sudo_ok = passwordless_sudo() if wants_sudo else False
    if reset_global and sudo_ok:
        # `ug configure --user` refuses to run while machine-wide managed settings exist.
        reset_global_state()
    results: dict[str, dict] = {}

    def payload() -> dict:
        ordered = []
        for command_id in commands:
            if command_id not in results:
                continue
            entry = dict(results[command_id])
            for side in binaries:
                raw = entry[side]
                # A launch whose untimed setup failed has no timed runs: count each as a failure.
                failures = iterations if raw.get("setup_failed") else raw["failures"]
                entry[side] = {
                    "ok": len(raw["samples"]),
                    "failures": failures,
                    "setup_failed": bool(raw.get("setup_failed")),
                    "summary": summarize(raw["samples"]),
                }
            ordered.append(entry)
        return {
            "version": SCHEMA_VERSION,
            "meta": {
                **(meta or {}),
                "iterations": iterations,
                "warmup": warmup,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "results": ordered,
        }

    def flush() -> None:
        if checkpoint:
            checkpoint(payload())

    for command_id in [c for c in EXECUTION_ORDER if c in commands]:
        flush()  # everything finished so far survives a later hang or job timeout
        spec = SPECS[command_id]
        entry: dict = {"id": command_id, "label": spec.label, "status": "ok"}
        sides = {side: {"samples": [], "failures": 0} for side in binaries}
        entry.update(sides)
        results[command_id] = entry
        if spec.needs_sudo and not sudo_ok:
            entry["status"] = "skipped"
            log(f"[{command_id}] skipped: passwordless sudo is unavailable")
            continue
        project = work_dir / command_id / "project"
        project.mkdir(parents=True)
        homes = {}
        for side, binary in binaries.items():
            home = work_dir / command_id / side / "home"
            home.mkdir(parents=True)
            homes[side] = home
            if spec.kind == "launch":
                env = isolated_environment(
                    home, stub_dir, os.environ, bearer, Path(binaries[side]).parent
                )
                _, code, err = timed_run(setup_argv(binary, workspace), env, project)
                if code != 0:
                    log(
                        f"[{command_id}/{side}] setup configure failed ({code}): "
                        f"{redact(err, secrets)}"
                    )
                    sides[side]["setup_failed"] = True
        for index in range(warmup + iterations):
            timed = index >= warmup
            for side in run_order(index):
                if spec.kind == "launch" and sides[side].get("setup_failed"):
                    continue
                if spec.kind == "configure":
                    # A fresh HOME per run: no saved state, caches or tokens carry over.
                    home = work_dir / command_id / side / f"run-{index}"
                    home.mkdir(parents=True)
                else:
                    home = homes[side]
                if spec.needs_sudo and reset_global:
                    reset_global_state()
                env = isolated_environment(
                    home, stub_dir, os.environ, bearer, Path(binaries[side]).parent
                )
                argv = build_argv(binaries[side], spec, workspace)
                elapsed, code, err = timed_run(argv, env, project)
                label = "run" if timed else "warmup"
                log(f"[{command_id}/{side}] {label} {index}: {elapsed:.3f}s exit={code}")
                if code != 0:
                    if timed:
                        sides[side]["failures"] += 1
                    log(redact(err, secrets))
                elif timed:
                    sides[side]["samples"].append(elapsed)
                if spec.kind == "configure":
                    shutil.rmtree(home, ignore_errors=True)
    if reset_global and sudo_ok:
        reset_global_state()
    flush()
    return payload()


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _absolute_executable(value: str) -> str:
    path = os.path.abspath(value)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise argparse.ArgumentTypeError(f"{value} is not an executable file")
    return path


def _iterations(value: str) -> int:
    try:
        return parse_iterations(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)

    run = sub.add_parser("measure", help="time the commands on both builds")
    run.add_argument("--before", required=True, type=_absolute_executable, help="base `ug` path")
    run.add_argument("--after", required=True, type=_absolute_executable, help="head `ug` path")
    run.add_argument("--workspace", required=True, help="Databricks workspace URL")
    run.add_argument("--iterations", type=_iterations, default=DEFAULT_ITERATIONS)
    run.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    run.add_argument(
        "--commands",
        default=",".join(DISPLAY_ORDER),
        help=f"comma-separated subset of: {', '.join(DISPLAY_ORDER)}",
    )
    run.add_argument("--base-sha", default="")
    run.add_argument("--head-sha", default="")
    run.add_argument("--output", required=True, type=Path, help="results JSON path")
    run.add_argument(
        "--reset-global",
        action="store_true",
        help="CI only: delete the root-owned managed settings before each global configure",
    )

    show = sub.add_parser("render", help="render the PR comment from a results file")
    show.add_argument("results", type=Path)
    show.add_argument("--output", required=True, type=Path)
    show.add_argument("--run-url", default=None)
    show.add_argument("--failure", action="store_true", help="render the failure comment instead")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "render":
        if args.failure:
            args.output.write_text(render_failure(args.run_url), encoding="utf-8")
            return 0
        data = json.loads(args.results.read_text(encoding="utf-8"))
        args.output.write_text(render_markdown(data, args.run_url), encoding="utf-8")
        return 0

    try:
        commands = parse_command_list(args.commands)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.reset_global and os.environ.get("GITHUB_ACTIONS") != "true":
        print("error: --reset-global is only allowed in GitHub Actions", file=sys.stderr)
        return 2
    bearer = os.environ.get("DATABRICKS_BEARER", "").strip() or None

    def save(data: dict) -> None:
        args.output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="ug-latency-") as tmp:
        data = measure(
            binaries={"before": args.before, "after": args.after},
            workspace=args.workspace.rstrip("/"),
            commands=commands,
            iterations=args.iterations,
            warmup=max(0, args.warmup),
            work_dir=Path(tmp),
            bearer=bearer,
            reset_global=args.reset_global,
            meta={"base_sha": args.base_sha, "head_sha": args.head_sha},
            checkpoint=save,
        )
    broken = [
        r
        for r in data["results"]
        if r["status"] != "skipped" and not (r["before"]["ok"] and r["after"]["ok"])
    ]
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
