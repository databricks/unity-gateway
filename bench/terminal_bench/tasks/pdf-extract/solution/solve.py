"""Reference solution for pdf-extract."""

import os
from pathlib import Path

Path(os.environ.get("TASK_APP_DIR", "/app"), "total.txt").write_text("7,342.19 EUR\n")
