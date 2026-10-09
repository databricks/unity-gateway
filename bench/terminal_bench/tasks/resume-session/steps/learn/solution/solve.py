"""Reference step 1: the oracle has no memory, so keep the value outside the project."""

import os
import tempfile
from pathlib import Path

vault = Path(os.environ.get("TASK_APP_DIR", "/app"), "vault.txt")
Path(tempfile.gettempdir(), "ug-bench-passphrase").write_text(vault.read_text().split(": ", 1)[1])
vault.unlink()
