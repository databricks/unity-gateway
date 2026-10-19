"""Reference step 2."""

import os
import tempfile
from pathlib import Path

value = Path(tempfile.gettempdir(), "ug-bench-code-word").read_text()
Path(os.environ.get("TASK_APP_DIR", "/app"), "answer.txt").write_text(value)
