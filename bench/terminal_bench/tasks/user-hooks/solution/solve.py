"""Reference run: drive the hook the way Claude Code would."""

import json
import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
hook = [sys.executable, str(app / ".claude" / "hooks" / "hook.py")]
out = subprocess.run([*hook, "prompt"], input="{}", capture_output=True, text=True, check=True)
codename = out.stdout.strip().removesuffix(".").rsplit(" ", 1)[-1]
(app / "release.txt").write_text(codename + "\n")
payload = {"tool_name": "Write", "tool_input": {"file_path": str(app / "release.txt")}}
subprocess.run([*hook, "tool"], input=json.dumps(payload), text=True, check=True)
