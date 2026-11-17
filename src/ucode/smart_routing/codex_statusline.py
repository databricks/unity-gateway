"""Session-local MCP footer for Codex's experimental status line providers."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TextIO

from ucode.smart_routing.session_env import SESSION_ENV_VAR, session_routing_enabled

MINIMUM_VERSION = (0, 171, 0)
MINIMUM_ALPHA = 12
SERVER_NAME = "ug-smart-router-statusline"
TOOL_NAME = "statusline"
RESOURCE_URI = "ui://ug-smart-router/footer"
STATE_META_KEY = "com.openai/codex-statusline-state"
TOOL = {
    "name": TOOL_NAME,
    "title": "Unity Gateway Smart Router",
    "description": "Show this session's Smart Router state in the Codex footer.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True},
    "_meta": {"openai/ui": {"entrypoints": [{"type": "statusline", "resourceUri": RESOURCE_URI}]}},
}


def supported_version(version: str) -> bool:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?(?:\+[\w.-]+)?", version)
    if match is None:
        return False
    release = tuple(int(part) for part in match.group(1, 2, 3))
    if release != MINIMUM_VERSION:
        return release > MINIMUM_VERSION
    stage, number = match.group(4, 5)
    return stage != "alpha" or int(number) >= MINIMUM_ALPHA


def launch_config(version: str, controls: Path) -> dict:
    if not supported_version(version):
        return {}
    return {
        "features.tui_extensions_statusline": True,
        "tui.status_line_provider": {"server": SERVER_NAME, "tool": TOOL_NAME},
        "mcp_servers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": ["-X", "utf8", "-m", __name__],
                "env": {SESSION_ENV_VAR: str(controls)},
                "enabled": True,
            }
        },
    }


def _text(value: str, color: str) -> dict:
    return {"type": "text", "props": {"color": color}, "children": [value]}


def render(state: dict[str, Any]) -> dict:
    controls = os.environ.get(SESSION_ENV_VAR)
    try:
        if not controls:
            raise ValueError("No session controls")
        enabled = session_routing_enabled(Path(controls), retry=False)
        label, color = ("ON", "green") if enabled else ("OFF", "yellow")
    except (OSError, UnicodeError, ValueError):
        label, color = "UNKNOWN", "yellow"

    thread = state.get("thread", {})
    model = thread.get("model", {})
    model_name = model.get("display_name") or model.get("id") or "Codex"
    if model.get("reasoning_level"):
        model_name += " " + model["reasoning_level"]
    children = [_text(f"smart router {label}", color), _text(model_name, "status-model")]
    cwd = thread.get("workspace", {}).get("cwd")
    if cwd:
        path = PureWindowsPath(cwd) if "\\" in cwd else PurePosixPath(cwd)
        children.append(_text(path.name or str(path), "status-path"))
    used = thread.get("usage", {}).get("context", {}).get("used_percentage")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        remaining = max(0, min(100, round(100 - used)))
        children.append(_text(f"{remaining}% context left", "status-usage"))
    separated: list = []
    for child in children:
        if separated:
            separated.append("·")
        separated.append(child)
    return {"type": "row", "props": {"gap": 1, "wrap": "wrap"}, "children": separated}


def respond(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            raise ValueError("Unknown tool")
        return {"content": [{"type": "text", "text": f"Footer resource: {RESOURCE_URI}"}]}
    if method == "resources/list":
        return {
            "resources": [{"name": "footer", "uri": RESOURCE_URI, "mimeType": "application/json"}]
        }
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        if params.get("uri") != RESOURCE_URI:
            raise ValueError("Unknown resource")
        state = params.get("_meta", {}).get(STATE_META_KEY, {})
        return {
            "contents": [
                {
                    "uri": RESOURCE_URI,
                    "mimeType": "application/json",
                    "text": json.dumps(render(state)),
                }
            ]
        }
    raise KeyError(method)


def serve(stdin: TextIO, stdout: TextIO) -> None:
    for line in stdin:
        reply: dict = {"jsonrpc": "2.0", "id": None}
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("method"), str):
                reply["error"] = {"code": -32600, "message": "Invalid request"}
            elif "id" not in request:
                continue
            else:
                reply["id"] = request["id"]
                reply["result"] = respond(request["method"], request.get("params", {}))
        except json.JSONDecodeError:
            reply["error"] = {"code": -32700, "message": "Parse error"}
        except KeyError:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        except (TypeError, ValueError, AttributeError):
            reply["error"] = {"code": -32602, "message": "Invalid parameters"}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


if __name__ == "__main__":
    serve(sys.stdin, sys.stdout)
