"""Measure fresh-process UG command startup with the current Python environment.

Run ``uv run --frozen scripts/benchmark_startup.py``. To compare source trees with
identical dependencies, pass ``--baseline-source-directory /path/to/baseline/src``.
The commands only import the CLI or request help/version; no agent is launched.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

from ucode.os_compatibility import subprocess_cross_os

_SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
_PREFIX = "import sys; sys.path.insert(0, sys.argv.pop(1)); "
_ENTRYPOINT = _PREFIX + (
    'sys.argv = ["ug", *sys.argv[1:]]; from ucode.entrypoint import main; main()'
)
_CASES = {
    "cli_import": (_PREFIX + "import ucode.cli", []),
    "version": (_ENTRYPOINT, ["--version"]),
    "help": (_ENTRYPOINT, ["--help"]),
    "claude_help": (_ENTRYPOINT, ["claude", "--help"]),
    "mcp_help": (_ENTRYPOINT, ["mcp", "--help"]),
}


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _measure(command: list[str], env: dict[str, str], timeout: float) -> float:
    started = time.perf_counter_ns()
    result = subprocess_cross_os.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if result.returncode != 0:
        raise RuntimeError(
            f"Startup command failed with exit {result.returncode}: {command!r}\n"
            f"{result.stderr[-4000:]}"
        )
    return elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-directory", type=Path, default=_SOURCE_DIRECTORY)
    parser.add_argument("--baseline-source-directory", type=Path)
    parser.add_argument(
        "--runs", type=int, default=11, help="Measured runs per command and source."
    )
    parser.add_argument("--timeout", type=float, default=30, help="Seconds allowed per process.")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    sources = {"current": args.source_directory.resolve()}
    if args.baseline_source_directory is not None:
        sources = {"baseline": args.baseline_source_directory.resolve(), **sources}
    for source in sources.values():
        if not all((source / "ucode" / name).is_file() for name in ("cli.py", "entrypoint.py")):
            parser.error(f"{source} must contain ucode/cli.py and ucode/entrypoint.py")

    env = {
        **os.environ,
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "120",
        "UG_LAUNCH_LATENCY_PATH": "",
    }
    commands = {
        variant: {
            name: [sys.executable, "-c", code, str(source), *argv]
            for name, (code, argv) in _CASES.items()
        }
        for variant, source in sources.items()
    }
    samples: dict[str, dict[str, list[float]]] = {
        variant: {name: [] for name in _CASES} for variant in sources
    }
    # Warm each command once, then interleave variants and reverse their order
    # each round to reduce bias from filesystem caches and changing machine load.
    try:
        for name in _CASES:
            for variant in sources:
                _measure(commands[variant][name], env, args.timeout)
        for run in range(args.runs):
            variants = list(sources) if run % 2 == 0 else list(reversed(sources))
            for name in _CASES:
                for variant in variants:
                    samples[variant][name].append(
                        _measure(commands[variant][name], env, args.timeout)
                    )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")

    dependencies = {}
    for distribution in ("unity-gateway", "typer", "rich", "questionary", "requests"):
        try:
            dependencies[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            dependencies[distribution] = None
    print(
        json.dumps(
            {
                "scope": (
                    "Local command startup including Python process initialization and exit; "
                    "excludes uv overhead, agent startup/readiness, authentication and gateway I/O."
                ),
                "environment": {
                    "python": sys.version,
                    "executable": sys.executable,
                    "platform": platform.platform(),
                    "installed_dependencies": dependencies,
                    "terminal": "dumb, 120 columns, no color",
                    "launch_latency_capture": False,
                },
                "runs": args.runs,
                "warmup_runs": 1,
                "p90_method": "Linear interpolation at (n - 1) * 0.9",
                "variants": {
                    variant: {
                        "source_directory": str(source),
                        "commands": {
                            name: {
                                "command": commands[variant][name],
                                "samples_ms": values,
                                "median_ms": statistics.median(values),
                                "p90_ms": _percentile(values, 0.9),
                            }
                            for name, values in samples[variant].items()
                        },
                    }
                    for variant, source in sources.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
