"""Measure the bare-launch budget fallback with controlled request latency.

Run ``uv run scripts/benchmark_startup_budget.py --runs 50``; use ``--source``
to select another checkout with the same interpreter/dependencies. All external
setup, authentication, file writes, and agent execution are replaced. Only the
budget request sleeps. This is a component experiment, not live startup p90.
"""

import argparse
import contextlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch


def measure(delay: float, source: Path) -> dict:
    import typer

    from ucode import cli
    from ucode.smart_routing.config import (
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
        SMART_ROUTING_ENV_KEYS,
    )

    if not Path(cli.__file__).resolve().is_relative_to(source / "src"):
        raise RuntimeError("loaded CLI is not from --source")

    state = {
        "workspace": "https://benchmark.invalid",
        "available_tools": ["claude"],
        "claude_models": {"sonnet": "system.ai.claude-sonnet-4-6"},
    }
    managed = {
        "default_agent": "claude",
        "enabled_agents": {
            "claude": {"model_config": {"default_model": "system.ai.claude-sonnet-4-6"}}
        },
        "smart_defaults": {"tiers": [{"spending_percentage": 0.8}]},
    }
    calls = 0

    def unavailable(*_args):
        nonlocal calls
        calls += 1
        time.sleep(delay)
        return None, "timed out (controlled benchmark)"

    def configured(_tool, launch_state, *_args, **_kwargs):
        return launch_state

    # Preserve the real budget helper, bare-agent selection, and _launch_tool decision.
    # Replace infrastructure boundaries so this never reads developer state or calls a service.
    replacements = {
        "_launch_workspace_state": lambda _workspace: state.copy(),
        "load_state": lambda: state.copy(),
        "install_databricks_cli": lambda **_kwargs: None,
        "apply_pat_environment": lambda _state: None,
        "refresh_managed_config": lambda _state: (managed, False),
        "ensure_bootstrap_dependencies": lambda *_args, **_kwargs: None,
        "ensure_provider_state": lambda _tool: state.copy(),
        "configure_shared_state": lambda *_args, **_kwargs: state.copy(),
        "configure_tool": configured,
        "refresh_downloaded_skills_on_launch": lambda _state: None,
        "get_databricks_token": lambda *_args: "benchmark-token",
        "get_model_recommendation": unavailable,
        "_print_managed_summary": lambda *_args, **_kwargs: None,
        "spinner": lambda *_args: contextlib.nullcontext(),
    }
    environment = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    environment[SMART_ROUTER_CONFIG_VERSION_ENV_VAR] = ""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment))
        stack.enter_context(patch.object(cli, "is_dry_run", return_value=False))
        stack.enter_context(
            patch.object(cli.claude_agent, "get_family_defaults", return_value=None)
        )
        for name, value in replacements.items():
            stack.enter_context(patch.object(cli, name, value))
        for name in ("print_note", "print_success", "print_section"):
            stack.enter_context(patch.object(cli, name))
        warnings = stack.enter_context(patch.object(cli, "print_warning"))
        launch = stack.enter_context(patch.object(cli, "launch_agent"))
        ctx = typer.Context(typer.main.get_command(typer.Typer(callback=lambda: None)))
        start = time.perf_counter()
        cli._launch_managed_default(ctx, dry_run=False, skip_preflight=False, workspace=None)
        elapsed_ms = (time.perf_counter() - start) * 1000
        launch.assert_called_once()
        assert launch.call_args.args[0] == "claude"
        budget_warnings = sum(
            "Could not check your budget" in call.args[0] for call in warnings.call_args_list
        )
    return {"elapsed_ms": elapsed_ms, "requests": calls, "warnings": budget_warnings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--baseline", type=Path, help="Alternate baseline/candidate fresh processes"
    )
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--delay-ms", type=float, default=50)
    args = parser.parse_args()
    if args.runs < 1 or not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error("--runs must be positive and --delay-ms must be finite and nonnegative")
    for source in (args.source, args.baseline):
        if source is not None and not (source / "src/ucode/cli.py").is_file():
            parser.error("each source must be a Unity Gateway checkout")
    sys.path.insert(0, str(args.source.resolve() / "src"))
    if args.baseline is not None:
        from ucode.os_compatibility import subprocess_cross_os

        sources = {"baseline": args.baseline.resolve(), "candidate": args.source.resolve()}
        samples = {label: [] for label in sources}
        revisions = {}
        for label, source in sources.items():
            revisions[label] = subprocess_cross_os.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        for index in range(args.runs):
            for label in sources if index % 2 == 0 else reversed(sources):
                child = subprocess_cross_os.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--source",
                        str(sources[label]),
                        "--runs",
                        "1",
                        "--delay-ms",
                        str(args.delay_ms),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=max(30, args.delay_ms / 1000 * 4 + 10),
                )
                samples[label].extend(json.loads(child.stdout)["samples"])
        print(
            json.dumps(
                {
                    "scope": "component: unavailable budget, alternating fresh processes",
                    "revisions": revisions,
                    "simulated_request_ms": args.delay_ms,
                    "warmups_per_process": 1,
                    "runs_per_revision": args.runs,
                    "percentile_method": "nearest-rank",
                    "results": {
                        label: {
                            "p50_ms": statistics.median(s["elapsed_ms"] for s in values),
                            "p90_ms": sorted(s["elapsed_ms"] for s in values)[
                                math.ceil(0.9 * len(values)) - 1
                            ],
                            "samples": values,
                        }
                        for label, values in samples.items()
                    },
                },
                indent=2,
            )
        )
        return
    from ucode.os_compatibility import subprocess_cross_os

    revision = subprocess_cross_os.run(
        ["git", "rev-parse", "HEAD"],
        cwd=args.source,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    measure(args.delay_ms / 1000, args.source.resolve())
    samples = [measure(args.delay_ms / 1000, args.source.resolve()) for _ in range(args.runs)]
    values = sorted(sample["elapsed_ms"] for sample in samples)
    print(
        json.dumps(
            {
                "scope": "component: bare launch with unavailable budget recommendation",
                "source": str(args.source.resolve()),
                "revision": revision,
                "simulated_request_ms": args.delay_ms,
                "warmups": 1,
                "runs": args.runs,
                "percentile_method": "nearest-rank",
                "p50_ms": statistics.median(values),
                "p90_ms": values[math.ceil(0.9 * len(values)) - 1],
                "max_ms": max(values),
                "samples": samples,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
