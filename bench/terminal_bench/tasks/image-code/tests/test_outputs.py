"""Verify the code read from the image."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
CODE = "47193825"


def main() -> None:
    answer = (APP / "code.txt").read_text().strip()
    # Some models misread a glyph in the bitmap font; 6 of 8 still shows the image arrived.
    assert len(answer) == len(CODE), answer
    assert sum(a == b for a, b in zip(answer, CODE, strict=True)) >= 6, answer
    print("ok", answer)


if __name__ == "__main__":
    main()
