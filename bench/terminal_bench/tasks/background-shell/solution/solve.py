"""Reference solution for background-shell."""

import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
job = subprocess.Popen([sys.executable, "slow_job.py"], cwd=app, stdout=subprocess.PIPE, text=True)
token = job.stdout.readline().strip().removeprefix("TOKEN=")
(app / "ack.txt").write_text(token + "\n")
out = job.stdout.read()
assert job.wait() == 0
(app / "result.txt").write_text(out.strip().removeprefix("RESULT=") + "\n")
