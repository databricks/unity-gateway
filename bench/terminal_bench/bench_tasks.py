"""List bench tasks for an agent. Tasks opt in with `[metadata] agents` in task.toml."""

import sys
import tomllib
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def task_agents(config: dict) -> list[str]:
    return config.get("metadata", {}).get("agents", ["claude", "codex"])


def names_for(agent: str) -> list[str]:
    return [
        task.name
        for task in sorted(TASKS_DIR.iterdir())
        if agent == "oracle"
        or agent in task_agents(tomllib.loads((task / "task.toml").read_text()))
    ]


if __name__ == "__main__":
    print("\n".join(names_for(sys.argv[1])))
