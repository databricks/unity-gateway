"""Reference solution for background-shell."""

import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
out = subprocess.run(
    [sys.executable, "slow_job.py"], cwd=app, capture_output=True, text=True, check=True
).stdout
(app / "result.txt").write_text(out.strip().removeprefix("RESULT=") + "\n")
