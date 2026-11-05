"""Redact Databricks bearers from every file under a directory before upload.

Per-task tokens are minted on the fly, so match token shapes as well as the
DATABRICKS_BEARER value.
"""

import os
import re
import sys
from pathlib import Path

TOKEN = re.compile(rb"eyJ[\w-]+\.[\w-]+\.[\w-]+|dapi[0-9a-f]{32}(?:-\d+)?")
secret = os.environ.get("DATABRICKS_BEARER", "").strip().encode()

for path in Path(sys.argv[1]).rglob("*"):
    if path.is_file() and not path.is_symlink():
        data = path.read_bytes()
        scrubbed = TOKEN.sub(b"***", data.replace(secret, b"***") if secret else data)
        if scrubbed != data:
            path.write_bytes(scrubbed)
