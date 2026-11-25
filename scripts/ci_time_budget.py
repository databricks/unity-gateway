"""Check a CI job's setup, run and total time budgets."""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budgets", type=Path, required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--started", type=int, required=True)
    parser.add_argument("--run-started", type=int, required=True)
    parser.add_argument("--ended", type=int, required=True)
    parser.add_argument("--changed-files", default="")
    args = parser.parse_args()

    budgets = json.loads(args.budgets.read_text(encoding="utf-8"))
    budget = budgets.get(args.workflow, {}).get(args.job, {}).get(args.name)
    if budget is None:
        print(f"::error::No time budget for {args.workflow}/{args.job}/{args.name}")
        return 1
    times = {
        "setup": args.run_started - args.started,
        "run": args.ended - args.run_started,
        "total": args.ended - args.started,
    }
    if any(value < 0 for value in times.values()):
        raise ValueError("CI budget clock moved backwards")
    print(", ".join(f"{phase}: {elapsed}s / {budget[phase]}s" for phase, elapsed in times.items()))
    over = [
        f"{phase} {elapsed}s > {budget[phase]}s"
        for phase, elapsed in times.items()
        if elapsed > budget[phase]
    ]
    if not over:
        return 0
    if args.changed_files:
        print(f"::notice::Over budget after dependency changes: {', '.join(over)}")
        return 0
    print(f"::error::Over budget: {', '.join(over)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
