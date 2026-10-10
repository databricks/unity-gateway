"""Reference solution for background-service."""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
proc = subprocess.Popen([sys.executable, "server.py"], cwd=app)
port_file = app / ".server_state" / "port"
for _ in range(100):
    if port_file.exists() and port_file.read_text():
        break
    time.sleep(0.1)
request = urllib.request.Request(
    f"http://127.0.0.1:{port_file.read_text()}/token", headers={"X-Bench-Client": "ug"}
)
(app / "token.txt").write_text(urllib.request.urlopen(request).read().decode())
proc.terminate()
proc.wait()
