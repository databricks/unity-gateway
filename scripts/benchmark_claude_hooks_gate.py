"""Measure Claude's gate check with controlled local lock contention.

Run ``uv run scripts/benchmark_claude_hooks_gate.py``. ``--source`` selects another
checkout with the same interpreter/dependencies. Temporary files and real local
locks isolate the component; no agent, workspace, or credentials are used. The
default 50 ms lock hold is a modeled competing writer, not production latency.
Imports, fixture setup, and lock-holder cleanup are outside the timed operation.
"""

import argparse
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path


def measure(path: Path, *, enabled: bool, hold_seconds: float | None) -> float:
    from ucode.agents import claude
    from ucode.os_compatibility.file_lock_cross_os import (
        acquire_exclusive_file_lock,
        release_file_lock,
    )

    before = {
        "cachedGrowthBookFeatures": {"tengu_plugin_hooks_modules": enabled},
        "numStartups": 7,
    }
    path.write_text(json.dumps(before), encoding="utf-8")
    ready = threading.Event()
    start_hold = threading.Event()
    errors: list[BaseException] = []

    def hold_lock():
        try:
            lock_path = path.with_name(path.name + claude.CLAUDE_CONFIG_LOCK_SUFFIX)
            with lock_path.open("a+b") as held:
                acquire_exclusive_file_lock(held, timeout=5)
                try:
                    ready.set()
                    if not start_hold.wait(5):
                        raise TimeoutError("benchmark did not start the gate check")
                    time.sleep(hold_seconds)
                finally:
                    release_file_lock(held)
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    holder = None
    if hold_seconds is not None:
        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
    try:
        if holder is not None and not ready.wait(5):
            raise TimeoutError("benchmark lock holder did not start")
        if errors:
            raise errors[0]
        start = time.perf_counter()
        start_hold.set()
        claude.ensure_hooks_modules_gate()
        elapsed_ms = (time.perf_counter() - start) * 1000
    finally:
        start_hold.set()
        if holder is not None:
            holder.join(timeout=10)
            if holder.is_alive():
                raise TimeoutError("benchmark lock holder did not stop")
    if errors:
        raise errors[0]
    before["cachedGrowthBookFeatures"]["tengu_plugin_hooks_modules"] = True
    if json.loads(path.read_text(encoding="utf-8")) != before:
        raise AssertionError("gate update changed unrelated config or failed to enable the gate")
    return elapsed_ms


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p90_ms": ordered[math.ceil(0.9 * len(samples)) - 1],
        "max_ms": max(samples),
        "samples_ms": samples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--lock-hold-ms", type=float, default=50)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if not math.isfinite(args.lock_hold_ms) or not 0 < args.lock_hold_ms <= 1000:
        parser.error("--lock-hold-ms must be finite, positive, and at most 1000")
    source = args.source.resolve()
    if not (source / "src/ucode/agents/claude.py").is_file():
        parser.error("--source must be a Unity Gateway checkout")
    sys.path.insert(0, str(source / "src"))

    from ucode.agents import claude
    from ucode.os_compatibility import subprocess_cross_os

    # Load the selected source before timing; a wrong editable install invalidates comparison.
    if not Path(claude.__file__).resolve().is_relative_to(source / "src"):
        raise RuntimeError("loaded Claude adapter is not from --source")
    revision = subprocess_cross_os.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    results = {}
    previous_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="ug-gate-benchmark-") as directory:
            os.environ["CLAUDE_CONFIG_DIR"] = directory
            path = Path(directory) / ".claude.json"
            for enabled in (True, False):
                for hold_seconds in (None, args.lock_hold_ms / 1000):
                    name = ("already_enabled" if enabled else "needs_update") + (
                        "_contended" if hold_seconds is not None else "_uncontended"
                    )
                    measure(path, enabled=enabled, hold_seconds=hold_seconds)
                    results[name] = summarize(
                        [
                            measure(path, enabled=enabled, hold_seconds=hold_seconds)
                            for _ in range(args.runs)
                        ]
                    )
    finally:
        if previous_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = previous_config_dir
    print(
        json.dumps(
            {
                "source": str(source),
                "revision": revision,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "controlled_lock_hold_ms": args.lock_hold_ms,
                "percentile_method": "nearest rank",
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
