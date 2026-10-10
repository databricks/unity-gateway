"""Verify stock.json matches what the MCP server actually returned."""

import json
import os
from pathlib import Path

APP = Path(os.environ.get("TASK_APP_DIR", "/app"))
SKUS = ["A-100", "B-200", "C-300"]


def main() -> None:
    log = APP / ".mcp_log" / "calls.jsonl"
    assert log.exists(), "the MCP tool was never called"
    served = {}
    for line in log.read_text().splitlines():
        call = json.loads(line)
        served[call["sku"]] = call["units"]
    actual = json.loads((APP / "stock.json").read_text())
    for sku in SKUS:
        assert sku in served, f"get_stock was never called for {sku}"
        assert actual.get(sku) == served[sku], {
            "sku": sku,
            "served": served[sku],
            "got": actual.get(sku),
        }
    print("ok")


if __name__ == "__main__":
    main()
