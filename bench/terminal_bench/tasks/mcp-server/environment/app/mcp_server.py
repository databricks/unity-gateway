"""Minimal stdio MCP server with one tool. Stock levels are random per server start."""

import json
import random
import sys
from pathlib import Path

LOG = Path(__file__).resolve().parent / ".mcp_log" / "calls.jsonl"
STOCK: dict[str, int] = {}
TOOL = {
    "name": "get_stock",
    "description": "Return the current number of units in stock for a SKU.",
    "inputSchema": {
        "type": "object",
        "properties": {"sku": {"type": "string"}},
        "required": ["sku"],
    },
}


def handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "inventory", "version": "1.0"},
        }
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        sku = params["arguments"]["sku"].strip().upper()
        units = STOCK.setdefault(sku, random.randint(100, 999))
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"sku": sku, "units": units}) + "\n")
        return {"content": [{"type": "text", "text": f"{sku}: {units} units in stock"}]}
    if method == "ping":
        return {}
    raise KeyError(method)


for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    try:
        response = {"result": handle(message["method"], message.get("params") or {})}
    except KeyError:
        response = {"error": {"code": -32601, "message": f"unknown method {message['method']}"}}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], **response}) + "\n")
    sys.stdout.flush()
