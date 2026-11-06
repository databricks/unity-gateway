"""Measure offline UG startup in fresh processes with an isolated home.

Run ``uv run --frozen scripts/profile_startup.py --help`` for options. Use
``--baseline-source`` to compare checkouts with the same Python environment.
Samples include interpreter startup; warmups warm the filesystem/bytecode cache,
not a persistent Python process. The auth-token case uses an inert bearer and
an invalid host; these cases do not sign in, configure an agent, or measure
native agent readiness or gateway latency.

``auth-full-cli`` is a control: it invokes the same auth-token command through
the full CLI to measure dedicated helper dispatch where a revision provides it.
On revisions without that dispatch, both auth cases run through the full CLI.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.system_cross_os import windows_system32_dir

try:
    import resource
except ImportError:  # Windows has no getrusage; wall-clock samples still work.
    resource = None

_ENTRYPOINT = "import sys; sys.argv[0] = 'ug'; from ucode.entrypoint import main; main()"
_INERT_BEARER = "ug-offline-benchmark-not-a-credential"
_CASES = {
    "python": ("pass", []),
    "version": (_ENTRYPOINT, ["--version"]),
    "help": (_ENTRYPOINT, ["--help"]),
    "claude-help": (_ENTRYPOINT, ["claude", "--help"]),
    "mcp-help": (_ENTRYPOINT, ["mcp", "--help"]),
    "auth-help": (_ENTRYPOINT, ["auth-token", "--help"]),
    "auth-token": (
        f"import os; os.environ['DATABRICKS_BEARER'] = {_INERT_BEARER!r}; {_ENTRYPOINT}",
        ["auth-token", "--host", "https://benchmark.invalid"],
    ),
    "auth-full-cli": (
        f"import os; os.environ['DATABRICKS_BEARER'] = {_INERT_BEARER!r}; "
        "import sys; sys.argv[0] = 'ug'; from ucode.cli import main; main()",
        ["auth-token", "--host", "https://benchmark.invalid"],
    ),
    "proxy-help": (_ENTRYPOINT, ["mcp-proxy", "--help"]),
    "cli-import": ("import ucode.cli", []),
}


def _environment(benchmark_home: Path, source: Path) -> dict[str, str]:
    # Pass only process/OS essentials. Inherited UG, agent, SDK and telemetry
    # settings (including credentials) must not change the benchmark's work.
    essentials = {"PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in essentials}
    if sys.platform == "win32":
        env["SystemRoot"] = str(windows_system32_dir().parent)
        env["HOMEDRIVE"] = benchmark_home.drive
        env["HOMEPATH"] = str(benchmark_home)[len(benchmark_home.drive) :]
    env.update(
        HOME=str(benchmark_home),
        USERPROFILE=str(benchmark_home),
        APPDATA=str(benchmark_home / "AppData" / "Roaming"),
        LOCALAPPDATA=str(benchmark_home / "AppData" / "Local"),
        XDG_CONFIG_HOME=str(benchmark_home / ".config"),
        XDG_CACHE_HOME=str(benchmark_home / ".cache"),
        XDG_DATA_HOME=str(benchmark_home / ".local" / "share"),
        XDG_STATE_HOME=str(benchmark_home / ".local" / "state"),
        PYTHONPATH=str(source / "src"),
        PYTHONNOUSERSITE="1",
        PYTHONHASHSEED="0",
        LANG="C.UTF-8",
        TERM="dumb",
        NO_COLOR="1",
        COLUMNS="100",
    )
    return env


def _child_cpu_seconds() -> float | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: float,
    expected_stdout: bytes | None = None,
) -> tuple[float, float | None, bytes]:
    cpu_before = _child_cpu_seconds()
    started = time.perf_counter_ns()
    result = subprocess_cross_os.run(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if expected_stdout is not None and result.stdout.strip() != expected_stdout:
        raise RuntimeError("Benchmark command returned unexpected stdout (contents withheld)")
    cpu_after = _child_cpu_seconds()
    cpu_ms = (
        max(0.0, cpu_after - cpu_before) * 1000
        if cpu_before is not None and cpu_after is not None
        else None
    )
    return elapsed, cpu_ms, result.stderr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1], help="UG checkout"
    )
    parser.add_argument(
        "--baseline-source", type=Path, help="Checkout to compare in alternating process order"
    )
    parser.add_argument(
        "--python", default=sys.executable, help="Python with UG's dependencies installed"
    )
    parser.add_argument("--runs", type=int, default=15, help="Measured fresh processes per case")
    parser.add_argument("--warmups", type=int, default=2, help="Unmeasured processes per case")
    parser.add_argument("--timeout", type=float, default=30, help="Seconds allowed per process")
    parser.add_argument("--case", action="append", choices=_CASES, help="Repeat to select cases")
    parser.add_argument("--json", type=Path, help="Write raw samples and environment metadata")
    parser.add_argument(
        "--profiles",
        type=Path,
        help="Write separate importtime and cProfile captures for each source/case",
    )
    args = parser.parse_args()
    sources = {"current": args.source.resolve()}
    if args.baseline_source:
        sources = {"baseline": args.baseline_source.resolve(), **sources}
    for source in sources.values():
        if not all(
            (source / "src" / "ucode" / name).is_file() for name in ("cli.py", "entrypoint.py")
        ):
            parser.error(
                f"{source} must be a UG checkout containing src/ucode/{{cli,entrypoint}}.py"
            )
    if args.runs < 1 or args.warmups < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--runs and --timeout must be positive; --warmups cannot be negative")
    # abspath deliberately retains a venv's Python symlink: resolving it would
    # select the base interpreter and lose the environment's dependencies.
    python = shutil.which(args.python)
    if python is None:
        parser.error("--python must name an available Python executable")
    python = os.path.abspath(python)
    names = list(dict.fromkeys(args.case or _CASES))
    profiles = args.profiles.resolve() if args.profiles else None
    report: dict[str, object] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "python_executable": python,
        "platform": platform.platform(),
        "runs": args.runs,
        "warmups": args.warmups,
        "p95_method": "nearest rank",
        "variants": {},
    }
    commands = {name: [python, "-c", _CASES[name][0], *_CASES[name][1]] for name in names}
    samples: dict[str, dict[str, list[float]]] = {
        variant: {name: [] for name in names} for variant in sources
    }
    cpu_samples: dict[str, dict[str, list[float]]] = {
        variant: {name: [] for name in names} for variant in sources
    }
    with tempfile.TemporaryDirectory(prefix="ug-startup-") as directory:
        homes = {variant: Path(directory) / variant for variant in sources}
        for benchmark_home in homes.values():
            benchmark_home.mkdir()
        environments = {
            variant: _environment(homes[variant], source) for variant, source in sources.items()
        }
        _, _, python_metadata = _run(
            [python, "-c", "import sys; print(sys.version, file=sys.stderr)"],
            env=environments["current"],
            cwd=homes["current"],
            timeout=args.timeout,
        )
        report["python_version"] = python_metadata.decode("utf-8", errors="replace").strip()
        # Alternate source and case order each round to reduce bias from cache
        # warmup and changes in machine load. Every observation is a fresh child.
        for iteration in range(args.warmups + args.runs):
            variants = list(sources)
            ordered_names = names
            if iteration % 2:
                variants.reverse()
                ordered_names = list(reversed(names))
            for name in ordered_names:
                for variant in variants:
                    elapsed, cpu_ms, _ = _run(
                        commands[name],
                        env=environments[variant],
                        cwd=homes[variant],
                        timeout=args.timeout,
                        expected_stdout=(
                            _INERT_BEARER.encode()
                            if name in {"auth-token", "auth-full-cli"}
                            else None
                        ),
                    )
                    if iteration >= args.warmups:
                        samples[variant][name].append(elapsed)
                        if cpu_ms is not None:
                            cpu_samples[variant][name].append(cpu_ms)
        print(f"{'Source / case':<29} {'Median (ms)':>12} {'p95 (ms)':>12} {'CPU (ms)':>12}")
        measurements = {}
        for variant, source in sources.items():
            cases = {}
            for name in names:
                values = samples[variant][name]
                median = statistics.median(values)
                p95 = sorted(values)[math.ceil(0.95 * len(values)) - 1]
                cpu_values = cpu_samples[variant][name]
                cpu_median = statistics.median(cpu_values) if cpu_values else None
                cases[name] = {
                    "command": commands[name],
                    "median_ms": median,
                    "p95_ms": p95,
                    "min_ms": min(values),
                    "samples_ms": values,
                    "median_child_cpu_ms": cpu_median,
                    "child_cpu_samples_ms": cpu_values,
                }
                cpu_display = f"{cpu_median:.2f}" if cpu_median is not None else "unavailable"
                print(f"{variant + ' / ' + name:<29} {median:12.2f} {p95:12.2f} {cpu_display:>12}")
                if profiles:
                    profile_directory = profiles / variant
                    profile_directory.mkdir(parents=True, exist_ok=True)
                    code, argv = _CASES[name]
                    expected_stdout = (
                        _INERT_BEARER.encode() if name in {"auth-token", "auth-full-cli"} else None
                    )
                    _, _, imports = _run(
                        [python, "-X", "importtime", "-c", code, *argv],
                        env=environments[variant],
                        cwd=homes[variant],
                        timeout=args.timeout,
                        expected_stdout=expected_stdout,
                    )
                    # Save interpreter diagnostics only, never arbitrary command output.
                    import_lines = [
                        line for line in imports.splitlines() if line.startswith(b"import time:")
                    ]
                    (profile_directory / f"{name}.importtime").write_bytes(
                        b"\n".join(import_lines) + b"\n"
                    )
                    profile_path = str(profile_directory / f"{name}.prof")
                    _run(
                        [
                            python,
                            "-c",
                            f"import cProfile; cProfile.run({code!r}, {profile_path!r})",
                            *argv,
                        ],
                        env=environments[variant],
                        cwd=homes[variant],
                        timeout=args.timeout,
                        expected_stdout=expected_stdout,
                    )
            measurements[variant] = {"source": str(source), "cases": cases}
        report["variants"] = measurements
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Offline startup only; fresh processes with warm filesystem caches.")


if __name__ == "__main__":
    main()
