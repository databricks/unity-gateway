"""Verify the release-notes skill's procedure was followed."""

import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    log = APP / ".skill_log" / "runs.jsonl"
    assert log.exists(), "the skill's build script never ran"
    assert any(
        json.loads(line)["version"].lstrip("v") == "2.3.0" for line in log.read_text().splitlines()
    )
    notes = (APP / "dist" / "release-notes-2.3.0.md").read_text().strip().splitlines()
    assert notes[0].strip() == "# Release 2.3.0", notes[0]
    assert notes[-1].strip() == "Signed-off-by: release-bot", notes[-1]
    body = "\n".join(notes)
    for item in [
        "Retry gateway calls on HTTP 429",
        "Smart routing for subagents",
        "Document managed settings",
    ]:
        assert item in body, item
    print("ok")


if __name__ == "__main__":
    main()
