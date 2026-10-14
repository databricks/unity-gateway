"""Project hook: share the release codename and log every hook call."""

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
payload = json.load(sys.stdin)
log = root / ".hook_log" / "events.jsonl"
log.parent.mkdir(exist_ok=True)
with log.open("a", encoding="utf-8") as f:
    f.write(
        json.dumps(
            {
                "event": sys.argv[1],
                "tool": payload.get("tool_name"),
                "file": (payload.get("tool_input") or {}).get("file_path"),
            }
        )
        + "\n"
    )
if sys.argv[1] == "prompt":
    codename = (root / ".claude" / "hooks" / "codename").read_text().strip()
    print(f"The release codename for this session is {codename}.")
