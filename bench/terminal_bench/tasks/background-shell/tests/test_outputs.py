"""Verify the agent acked the job while it ran, waited for it, and captured its output."""

import glob
import hashlib
import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def claude_tool_calls() -> list[dict]:
    """Claude's tool calls from the agent log; empty for Codex, whose log isn't stream-json."""
    paths = [os.environ.get("TASK_AGENT_LOG", "")] + glob.glob("/logs/agent/ug-claude.txt")
    calls = []
    for path in (Path(p) for p in paths if p and Path(p).exists()):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "assistant":
                content = event.get("message", {}).get("content", [])
                calls += [block for block in content if block.get("type") == "tool_use"]
    return calls


def main() -> None:
    state = APP / ".job_state"
    assert (state / "acked").exists(), "slow_job.py never saw its token in ack.txt"
    assert (state / "result.sha256").exists(), "slow_job.py never finished"
    value = (APP / "result.txt").read_text().strip().removeprefix("RESULT=")
    assert hashlib.sha256(value.encode()).hexdigest() == (state / "result.sha256").read_text()
    calls = claude_tool_calls()
    if calls:
        assert any(
            call["input"].get("run_in_background")
            and "slow_job.py" in call["input"].get("command", "")
            for call in calls
        ), "Claude didn't start slow_job.py as a background task"
    print("ok")


if __name__ == "__main__":
    main()
