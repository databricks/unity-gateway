"""Reference solution: follow the skill."""

import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
draft = subprocess.run(
    [sys.executable, ".claude/skills/release-notes/build.py", "2.3.0"],
    cwd=app,
    capture_output=True,
    text=True,
    check=True,
).stdout
(app / "dist").mkdir(exist_ok=True)
(app / "dist" / "release-notes-2.3.0.md").write_text(
    draft.rstrip() + "\n\nSigned-off-by: release-bot\n"
)
