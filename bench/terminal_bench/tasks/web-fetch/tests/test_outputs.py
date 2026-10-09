"""Verify the value fetched from the pinned file."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    answer = (APP / "answer.txt").read_text().strip().strip("\"'")
    assert answer == "Bootstrap Codex against a Databricks workspace with minimal setup.", answer
    print("ok")


if __name__ == "__main__":
    main()
