"""Reference step 1: the oracle has no memory, so keep the value outside the project."""

import os
import tempfile
from pathlib import Path

note = Path(os.environ.get("TASK_APP_DIR", "/app"), "note.txt")
Path(tempfile.gettempdir(), "ug-bench-code-word").write_text(note.read_text().split(": ", 1)[1])
note.unlink()
