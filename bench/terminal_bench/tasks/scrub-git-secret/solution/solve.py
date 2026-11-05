"""Reference solution for scrub-git-secret using filter-branch."""

import os
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
token = "ugb_live_7f3a9c2e1d4b8a6f0e5d"
# Python one-liner keeps the tree filter portable across Linux and Git Bash.
tree_filter = (
    f'"{Path(sys.executable).as_posix()}" -c "'
    "import pathlib;"
    "[p.write_text(p.read_text().replace('" + token + "','REDACTED'), newline='\\n')"
    " for p in pathlib.Path('.').rglob('*') if p.is_file() and '" + token + "' in p.read_text()]\""
)
env = {**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"}
subprocess.run(
    ["git", "filter-branch", "-f", "--tree-filter", tree_filter, "--", "--all"],
    cwd=app,
    check=True,
    env=env,
)
refs = subprocess.run(
    ["git", "for-each-ref", "--format=%(refname)", "refs/original"],
    cwd=app,
    check=True,
    capture_output=True,
    text=True,
).stdout.split()
for ref in refs:
    subprocess.run(["git", "update-ref", "-d", ref], cwd=app, check=True)
