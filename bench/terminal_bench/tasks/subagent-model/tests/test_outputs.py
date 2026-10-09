"""Verify the audits and that a subagent ran on the haiku model."""

import glob
import hashlib
import json
import os
import re
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def agent_log() -> Path:
    paths = [os.environ.get("TASK_AGENT_LOG", "")] + glob.glob("/logs/agent/ug-claude.txt")
    return next(Path(p) for p in paths if p and Path(p).exists())


def read_transcript(log: Path) -> tuple[set[str], list[str]]:
    """Models subagents ran on, and any notices that their requested model was restricted."""
    models, restricted = set(), []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant" and event.get("parent_tool_use_id"):
            models.add(event.get("message", {}).get("model", ""))
        if str(event.get("key", "")).startswith("agent-model-restricted-checksum-auditor"):
            restricted.append(event.get("text", ""))
    return models, restricted


def main() -> None:
    for path in sorted((APP / "data").iterdir()):
        audit = (APP / "audit" / f"{path.name}.txt").read_text().splitlines()
        assert audit[0].strip() == "auditor: checksum-auditor", audit
        assert audit[1].strip() == f"sha256: {hashlib.sha256(path.read_bytes()).hexdigest()}", audit
    if os.environ.get("TASK_ORACLE"):
        print("ok (oracle: skipped subagent check)")
        return
    models, restricted = read_transcript(agent_log())
    assert models, "no subagent ran"
    if any("haiku" in m for m in models):
        print("ok", models)
        return
    # A workspace policy can disallow haiku; then the subagent must run on the named fallback.
    assert restricted, f"subagent ignored model: haiku without a policy notice: {models}"
    fallback = re.search(r"Using (\S+?) instead", restricted[0])[1]
    assert all(fallback.endswith(m) for m in models), {"fallback": fallback, "models": models}
    print("ok (haiku restricted by policy)", fallback, models)


if __name__ == "__main__":
    main()
