"""Verify the code read from the image."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    assert (APP / "code.txt").read_text().strip() == "47193825"
    print("ok")


if __name__ == "__main__":
    main()
