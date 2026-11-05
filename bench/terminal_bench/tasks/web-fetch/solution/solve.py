"""Reference solution for web-fetch."""

import os
import tomllib
import urllib.request
from pathlib import Path

url = (
    "https://raw.githubusercontent.com/databricks/unity-gateway/"
    "ba0d79ed9d429e08af3e7cf110e379c004a3a503/pyproject.toml"
)
with urllib.request.urlopen(url, timeout=30) as response:
    description = tomllib.loads(response.read().decode())["project"]["description"]
Path(os.environ.get("TASK_APP_DIR", "/app"), "answer.txt").write_text(description + "\n")
