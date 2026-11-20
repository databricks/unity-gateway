"""Compare model-discovery fallback latency with controlled read delays.

Run from the candidate checkout with its dependencies installed::

    uv run python scripts/benchmark_model_fallbacks.py --baseline /path/to/base-checkout

Each sample runs the real configure_shared_state in a fresh process, replacing auth,
network, state persistence, and presentation boundaries. Only its execution is timed;
Python imports are excluded. No credentials, workspace, or installed agents are used.
The result is a component benchmark, not end-to-end startup or production p90.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import platform
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch


def sample(checkout: Path, tool: str, delay_seconds: float) -> float:
    sys.path.insert(0, str(checkout / "src"))
    from ucode import cli
    from ucode.databricks import GatewayProbe

    assert Path(cli.__file__).resolve().is_relative_to(checkout.resolve())
    workspace = "https://benchmark.example"
    models = {
        "claude": {"sonnet": "databricks-claude-sonnet-4-6"},
        "gemini": ["databricks-gemini-3-flash"],
        "codex": ["databricks-gpt-5"],
    }
    calls: list[str] = []

    def discover(family):
        calls.append(family)
        time.sleep(delay_seconds)
        return models[family], None

    with contextlib.ExitStack() as stack:
        for name, replacement in {
            "resolve_workspace": lambda w: w,
            "load_state": lambda: {"workspace": workspace},
            "load_full_state": lambda: {},
            "workspace_self_managed_agents": lambda w: [],
            "save_state": lambda state: None,
            "_purge_after_workspace_switch": lambda *args: None,
            "ensure_databricks_auth": lambda *args, **kwargs: None,
            "get_databricks_token": lambda *args: "benchmark-token",
            "probe_unity_gateway_capabilities": lambda *args: GatewayProbe(True, "fixture", True),
            "discover_model_services": lambda *args: ({}, [], [], [], None),
            "spinner": lambda *args: contextlib.nullcontext(),
            "print_success": lambda *args: None,
        }.items():
            stack.enter_context(patch.object(cli, name, replacement))
        for family in models:
            stack.enter_context(
                patch.object(
                    cli,
                    f"discover_{family}_models",
                    lambda *args, family=family: discover(family),
                )
            )
        start = time.perf_counter()
        state = cli.configure_shared_state(workspace, profile="benchmark", tools=[tool])
        elapsed_ms = (time.perf_counter() - start) * 1000
    expected = {"claude", "codex"} if tool == "claude" else set(models)
    assert sorted(calls) == sorted(expected), calls
    for family in expected:
        assert state[f"{family}_models"] == models[family]
    return elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, default=50)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--tool", choices=("claude", "pi"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.samples < 1 or args.warmups < 0:
        parser.error("--samples must be positive and --warmups nonnegative")
    if not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error("--delay-ms must be finite and nonnegative")
    if args.worker is not None:
        if args.tool is None:
            parser.error("a worker needs --tool")
        print(json.dumps(sample(args.worker, args.tool, args.delay_ms / 1000)))
        return
    if args.baseline is None:
        parser.error("--baseline is required")

    from ucode.os_compatibility import subprocess_cross_os

    checkouts = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    revisions = {}
    for label, checkout in checkouts.items():
        if not (checkout / "src/ucode/cli.py").is_file():
            parser.error(f"{label} is not a Unity Gateway checkout: {checkout}")
        revisions[label] = subprocess_cross_os.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    measurements = {tool: {label: [] for label in checkouts} for tool in ("claude", "pi")}
    for index in range(args.warmups + args.samples):
        # Alternate order to distribute host drift across both revisions.
        labels = list(checkouts) if index % 2 == 0 else list(reversed(checkouts))
        for tool in measurements:
            for label in labels:
                completed = subprocess_cross_os.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(checkouts[label]),
                        "--tool",
                        tool,
                        "--delay-ms",
                        str(args.delay_ms),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=max(30, args.delay_ms / 1000 * 3 + 10),
                )
                if index >= args.warmups:
                    measurements[tool][label].append(json.loads(completed.stdout))
    result = {
        "kind": "controlled component benchmark; imports/auth/network/persistence excluded",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "revisions": revisions,
        "delay_ms_per_missing_family": args.delay_ms,
        "samples": args.samples,
        "warmups": args.warmups,
        "percentile_method": "nearest rank",
        "results": {},
    }
    for tool, cases in measurements.items():
        result["results"][tool] = {
            label: {
                "p50_ms": statistics.median(samples),
                "p90_ms": sorted(samples)[math.ceil(0.9 * len(samples)) - 1],
                "samples_ms": samples,
            }
            for label, samples in cases.items()
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
