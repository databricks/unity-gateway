"""Reference run: speak MCP to the server over stdio."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

app = Path(os.environ.get("TASK_APP_DIR", "/app"))
requests = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}]
requests += [
    {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": "get_stock", "arguments": {"sku": sku}},
    }
    for i, sku in enumerate(["A-100", "B-200", "C-300"], start=1)
]
out = subprocess.run(
    [sys.executable, "mcp_server.py"],
    cwd=app,
    input="".join(json.dumps(r) + "\n" for r in requests),
    capture_output=True,
    text=True,
    check=True,
).stdout
stock = {}
for line in out.splitlines()[1:]:
    sku, units = re.match(
        r"(\S+): (\d+)", json.loads(line)["result"]["content"][0]["text"]
    ).groups()
    stock[sku] = int(units)
(app / "stock.json").write_text(json.dumps(stock))
