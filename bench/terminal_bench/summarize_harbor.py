"""Summarize a Harbor jobs dir as a markdown table.

Without --min-passes, exits non-zero when a trial errored for a reason other than a
timeout, since that points at the ug harness rather than the agent's score. With
--min-passes, every task must reach that many passing attempts.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def main(jobs_dir: Path, title: str, min_passes: int | None) -> int:
    attempts: dict[str, list[tuple[object, str]]] = defaultdict(list)
    harness_errors = 0
    for path in sorted(jobs_dir.glob("*/*/result.json")):
        result = json.loads(path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        error = (result.get("exception_info") or {}).get("exception_type", "")
        if error and "Timeout" not in error:
            harness_errors += 1
        attempts[result["task_name"]].append((rewards.get("reward", "-"), error or "-"))

    passes = {task: sum(reward == 1 for reward, _ in runs) for task, runs in attempts.items()}
    if min_passes is None:
        failed = harness_errors > 0
        heading = f"{sum(passes.values())}/{sum(map(len, attempts.values()))} solved"
    else:
        failing = [task for task, n in passes.items() if n < min_passes]
        failed = bool(failing)
        heading = f"{len(attempts) - len(failing)}/{len(attempts)} tasks passed"
        heading += f" (each needs {min_passes} passing attempts)"
    lines = [
        f"### {title}: {heading}",
        "",
        "| Task | Passed | Rewards | Errors |",
        "|------|--------|---------|--------|",
        *(
            f"| {task} | {passes[task]}/{len(runs)} | {', '.join(str(r) for r, _ in runs)} "
            f"| {', '.join(e for _, e in runs)} |"
            for task, runs in sorted(attempts.items())
        ),
    ]
    summary = "\n".join(lines) + "\n"
    print(summary)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)
    return 1 if failed or not attempts else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs_dir", type=Path)
    parser.add_argument("title")
    parser.add_argument("--min-passes", type=int)
    args = parser.parse_args()
    sys.exit(main(args.jobs_dir, args.title, args.min_passes))
