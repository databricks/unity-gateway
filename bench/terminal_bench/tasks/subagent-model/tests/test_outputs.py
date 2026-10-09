"""Verify the audits and that a subagent ran on the haiku model."""

import glob
import hashlib
import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))


def agent_log() -> Path:
    paths = [os.environ.get("TASK_AGENT_LOG", "")] + glob.glob("/logs/agent/ug-claude.txt")
    return next(Path(p) for p in paths if p and Path(p).exists())


def subagent_models(log: Path) -> set[str]:
    models = set()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "assistant" and event.get("parent_tool_use_id"):
            models.add(event.get("message", {}).get("model", ""))
    return models


def main() -> None:
    for path in sorted((APP / "data").iterdir()):
        audit = (APP / "audit" / f"{path.name}.txt").read_text().splitlines()
        assert audit[0].strip() == "auditor: checksum-auditor", audit
        assert audit[1].strip() == f"sha256: {hashlib.sha256(path.read_bytes()).hexdigest()}", audit
    if os.environ.get("TASK_ORACLE"):
        print("ok (oracle: skipped subagent check)")
        return
    models = subagent_models(agent_log())
    assert any("haiku" in m for m in models), f"no subagent ran on a haiku model: {models}"
    print("ok", models)


if __name__ == "__main__":
    main()
