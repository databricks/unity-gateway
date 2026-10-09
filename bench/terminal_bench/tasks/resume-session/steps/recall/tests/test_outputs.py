"""Verify the resumed session recalled the passphrase."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    assert "amber-falcon-7342" in (APP / "answer.txt").read_text()
    print("ok")


if __name__ == "__main__":
    main()
