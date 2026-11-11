"""VS Code chat evidence, read from the chat sessions Copilot Chat saves for the workspace."""

import json
import time
from pathlib import Path

from ..vscode import vscode_user_dir

__all__ = ["chat_requests"]

# VS Code saves chat sessions on a storage timer of about a minute, not when a turn ends.
SAVE_TIMEOUT_S = 120


def _replay(path: Path) -> dict | None:
    """Rebuild a session from its log: a snapshot (kind 0), then sets (1), splices (2), deletes (3)."""
    state = None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("kind") == 0:
            state = record["v"]
            continue
        if state is None:
            continue
        keys = record.get("k") or []
        if not keys:
            if record.get("kind") == 1:
                state = record["v"]
            continue
        parent = state
        for index, key in enumerate(keys[:-1]):
            if isinstance(parent, dict) and key not in parent:
                parent[key] = [] if isinstance(keys[index + 1], int) else {}
            parent = parent[key]
        last = keys[-1]
        if record["kind"] == 1:
            parent[last] = record["v"]
        elif record["kind"] == 2:
            target = parent[last]
            if isinstance(target, list) and isinstance(record.get("i"), int):
                target[record["i"] :] = record.get("v") or []
            elif isinstance(target, list):
                target.extend(record.get("v") or [])
            else:
                parent[last] = record.get("v")
        elif record["kind"] == 3:
            parent.pop(last, None)
    return state


def _summary(request: dict) -> dict:
    parts = request.get("response") or []
    return {
        "model": request.get("modelId"),
        "prompt": (request.get("message") or {}).get("text"),
        "tools": [
            part.get("toolId") for part in parts if part.get("kind") == "toolInvocationSerialized"
        ],
        "error": (request.get("result") or {}).get("errorDetails"),
        "text": "".join(
            part["value"]
            for part in parts
            if isinstance(part.get("value"), str) and not part.get("kind")
        )
        or "".join(
            (part.get("content") or {}).get("value", "")
            for part in parts
            if part.get("kind") == "markdownContent"
        ),
    }


def chat_requests(session, count: int) -> list[dict]:
    """Wait until VS Code has saved a chat with ``count`` completed requests; return them in order."""
    root = vscode_user_dir(session) / "User/workspaceStorage"
    deadline = time.monotonic() + SAVE_TIMEOUT_S
    while time.monotonic() < deadline:
        for path in root.glob("*/chatSessions/*.jsonl"):
            state = _replay(path)
            requests = (state or {}).get("requests") or []
            if len(requests) >= count and all(request.get("result") for request in requests):
                summaries = [_summary(request) for request in requests]
                session.record("vscode-chat-session.json", summaries)
                return summaries
        time.sleep(1)
    raise AssertionError(
        f"VS Code saved no chat with {count} completed requests in {SAVE_TIMEOUT_S}s"
    )
