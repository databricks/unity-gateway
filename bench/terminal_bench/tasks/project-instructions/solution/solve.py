"""Reference solution for project-instructions."""

import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
(app / "out").mkdir(exist_ok=True)
script = app / "out" / "count_words.py"
script.write_text(
    "# SPDX-License-Identifier: Apache-2.0\n"
    "from pathlib import Path\n"
    "Path('out/count.txt').write_text(str(len(Path('data.txt').read_text().split())))\n"
)
subprocess.run([sys.executable, str(script)], cwd=app, check=True)
