"""Verify the total read from the PDF."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    total = (APP / "total.txt").read_text().strip()
    assert total.removeprefix("Total due:").strip() == "7,342.19 EUR", total
    print("ok")


if __name__ == "__main__":
    main()
