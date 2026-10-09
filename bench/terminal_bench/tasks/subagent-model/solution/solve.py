"""Reference solution for subagent-model (the file side only)."""

import hashlib
import os
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
(app / "audit").mkdir(exist_ok=True)
for path in (app / "data").iterdir():
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (app / "audit" / f"{path.name}.txt").write_text(
        f"auditor: checksum-auditor\nsha256: {digest}\n"
    )
