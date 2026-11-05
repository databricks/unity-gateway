"""Verify the audits and that one subagent ran per file."""

import glob
import hashlib
import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def agent_log() -> Path:
    paths = [os.environ.get("TASK_AGENT_LOG", "")] + glob.glob("/logs/agent/ug-claude.txt")
    return next(Path(p) for p in paths if p and Path(p).exists())


def subagent_ids(log: Path) -> set[str]:
    ids = set()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant" and event.get("parent_tool_use_id"):
            ids.add(event["parent_tool_use_id"])
    return ids


def main() -> None:
    files = sorted((APP / "data").iterdir())
    for path in files:
        audit = (APP / "audit" / f"{path.name}.txt").read_text().strip()
        assert audit == f"sha256: {hashlib.sha256(path.read_bytes()).hexdigest()}", audit
    if os.environ.get("TASK_ORACLE"):
        print("ok (oracle: skipped subagent check)")
        return
    ids = subagent_ids(agent_log())
    assert len(ids) >= len(files), f"{len(ids)} subagents ran for {len(files)} files"
    print("ok", len(ids), "subagents")


if __name__ == "__main__":
    main()
