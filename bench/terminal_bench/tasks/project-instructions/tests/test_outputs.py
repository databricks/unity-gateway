"""Verify the agent followed the project's instruction file."""

import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
HEADER = "# SPDX-License-Identifier: Apache-2.0"


def main() -> None:
    expected = len((APP / "data.txt").read_text().split())
    assert not (APP / "count.txt").exists(), "count.txt was written to the project root"
    assert (APP / "out" / "count.txt").read_text().strip() == str(expected)
    scripts = [p for p in APP.rglob("*.py") if ".claude" not in p.parts]
    assert scripts, "no Python script was written"
    for script in scripts:
        assert script.read_text().splitlines()[0].strip() == HEADER, (
            f"{script.name} lacks the SPDX header"
        )
    print("ok")


if __name__ == "__main__":
    main()
