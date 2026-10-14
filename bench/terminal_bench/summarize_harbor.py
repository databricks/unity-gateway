"""Summarize a Harbor jobs dir as a markdown table.

Exits non-zero when a trial errored for a reason other than a timeout, since
that points at the ug harness rather than the agent's score.
"""

import json
import os
import sys
from pathlib import Path


def main(jobs_dir: Path, title: str) -> int:
    rows = []
    harness_errors = 0
    for path in sorted(jobs_dir.glob("*/*/result.json")):
        result = json.loads(path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        error = (result.get("exception_info") or {}).get("exception_type", "")
        if error and "Timeout" not in error:
            harness_errors += 1
        rows.append((result["task_name"], rewards.get("reward", "-"), error or "-"))

    solved = sum(1 for _, reward, _ in rows if reward == 1)
    lines = [
        f"### {title}: {solved}/{len(rows)} solved",
        "",
        "| Task | Reward | Error |",
        "|------|--------|-------|",
        *(f"| {task} | {reward} | {error} |" for task, reward, error in rows),
    ]
    summary = "\n".join(lines) + "\n"
    print(summary)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)
    return 1 if harness_errors or not rows else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2]))
