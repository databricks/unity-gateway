"""Render animal labels in Claude's native subagent panel (stdlib only)."""

import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import quote


def rows(payload, root):
    if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str):
        return
    tasks = payload.get("tasks", [])
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if not isinstance(task, dict) or task.get("type") != "local_agent":
            continue
        agent_id = task.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        # Match encodeURIComponent in the mod; the prefix also makes dot IDs safe.
        escaped_id = quote(agent_id, safe="~()*!.'")
        path = root / "agent-names" / f"agent-{escaped_id}.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(saved, dict) or saved.get("session_id") != payload["session_id"]:
            continue
        name = saved.get("name")
        if not isinstance(name, str) or not name:
            continue
        parts = [name, task.get("label") or task.get("description") or ""]
        tokens = task.get("tokenCount")
        if isinstance(tokens, (int, float)) and math.isfinite(tokens) and tokens >= 0:
            parts.append(f"{int(tokens):,} tokens")
        start = task.get("startTime")
        if (
            task.get("status") == "running"
            and isinstance(start, (int, float))
            and math.isfinite(start)
        ):
            parts.append(f"{max(0, int(time.time() - start / 1000))}s")
        # Content is rendered verbatim by Claude; never forward terminal control codes.
        content = " · ".join(
            "".join(c for c in part if c.isprintable())
            for part in parts
            if isinstance(part, str) and part
        )
        yield {"id": agent_id, "content": content}


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = None
    for row in rows(payload, Path(__file__).resolve().parent.parent):
        print(json.dumps(row))
