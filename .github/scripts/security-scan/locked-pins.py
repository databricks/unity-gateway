#!/usr/bin/env python3
"""Print `name==version` for every registry package pinned in a uv.lock.

Reads the lockfile directly instead of `uv export`, which would read the PR's
uv configuration and could build the PR's project. Called by
.github/workflows/security-scan.yml.

Usage: locked-pins.py <uv.lock>   (missing or empty file prints nothing)
"""

import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink():
    sys.exit("::error::uv.lock must be a regular file, not a symbolic link.")
text = path.read_text() if path.is_file() else ""
for package in tomllib.loads(text).get("package", []):
    if "registry" in package.get("source", {}):
        print(f"{package['name']}=={package['version']}")
