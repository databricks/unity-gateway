"""Reference solution for image-code."""

import os
from pathlib import Path

Path(os.environ.get("TASK_APP_DIR", "/app"), "code.txt").write_text("47193825\n")
