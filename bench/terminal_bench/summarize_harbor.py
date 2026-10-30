"""Summarize a Harbor jobs dir as a markdown table.

Exits non-zero unless every task reaches --min-passes passing attempts. A trial that
errored counts as a failed attempt. With --model, so does a trial whose Claude session
ran another model. With --undecided, prints instead the tasks that haven't met
--min-passes but still could within --attempts total runs.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def session_models(trial: Path) -> set[str]:
    """Models from Claude's stream-json `system`/`init` events. Codex's log has none."""
    models = set()
    for log in trial.glob("agent/ug-claude*.txt"):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                models.add(event.get("model", ""))
    return models


def same_model(a: str, b: str) -> bool:
    # ug launches GPT models with Claude Code's `[1m]` suffix.
    return a.lower().removesuffix("[1m]") == b.lower().removesuffix("[1m]")


def main(
    jobs_dir: Path, title: str, min_passes: int, undecided: bool, max_attempts: int, model: str
) -> int:
    attempts: dict[str, list[tuple[object, str]]] = defaultdict(list)
    for path in sorted(jobs_dir.glob("*/*/result.json")):
        result = json.loads(path.read_text())
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        reward = rewards.get("reward", "-")
        error = (result.get("exception_info") or {}).get("exception_type", "")
        # Claude Code swaps in its default when the workspace doesn't allow the pinned model.
        if model and any(not same_model(m, model) for m in session_models(path.parent)):
            reward, error = 0.0, error or "ModelMismatch"
        attempts[result["task_name"]].append((reward, error or "-"))

    passes = {task: sum(reward == 1 for reward, _ in runs) for task, runs in attempts.items()}
    failing = [task for task, n in passes.items() if n < min_passes]
    if undecided:
        for task in failing:
            if passes[task] + max_attempts - len(attempts[task]) >= min_passes:
                print(task)
        return 0
    lines = [
        f"### {title}: {len(attempts) - len(failing)}/{len(attempts)} tasks passed"
        f" (each needs {min_passes} passing attempts)",
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
    return 1 if failing or not attempts else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobs_dir", type=Path)
    parser.add_argument("title", nargs="?", default="")
    parser.add_argument("--min-passes", type=int, default=2)
    parser.add_argument("--undecided", action="store_true")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--model", default="", help="Fail trials whose session ran another model")
    args = parser.parse_args()
    sys.exit(
        main(
            args.jobs_dir,
            args.title,
            args.min_passes,
            args.undecided,
            args.attempts,
            args.model,
        )
    )
