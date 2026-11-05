"""Verify the codename came from the prompt hook and the write went through the tool hook."""

import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def main() -> None:
    assert (APP / "release.txt").read_text().strip() == "velvet-harbor-58"
    log = APP / ".hook_log" / "events.jsonl"
    assert log.exists(), "no project hook ran"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(e["event"] == "prompt" for e in events), "UserPromptSubmit hook didn't run"
    assert any(
        e["event"] == "tool" and (e["file"] or "").replace("\\", "/").endswith("release.txt")
        for e in events
    ), f"PostToolUse hook didn't see release.txt being written: {events}"
    print("ok")


if __name__ == "__main__":
    main()
