"""Session-local MCP footer for Codex's experimental status line providers."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TextIO

from ucode.smart_routing.session_env import (
    AUTO_MODEL,
    SESSION_ENV_VAR,
    auto_state_path,
    read_validation_result,
    session_routing_enabled,
    validation_dismissed_path,
    validation_result_path,
    validation_warning,
)

SERVER_NAME = "ug-smart-router-statusline"
TOOL_NAME = "statusline"
RESOURCE_URI = "ui://ug-smart-router/footer"
LOGS_DETAILS_HINT = "($smart-router logs for details)"
VALIDATION_PENDING_SECONDS = 11
STATE_META_KEY = "com.openai/codex-statusline-state"
TOOL = {
    "name": TOOL_NAME,
    "title": "Unity Gateway Smart Router",
    "description": "Show this session's Smart Router state in the Codex footer.",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True},
    "_meta": {
        "ui": {"visibility": ["app"]},
        "openai/ui": {"entrypoints": [{"type": "statusline", "resourceUri": RESOURCE_URI}]},
    },
}


def launch_config(controls: Path) -> dict:
    return {
        "features.tui_extensions_statusline": True,
        "tui.status_line_provider": {"server": SERVER_NAME, "tool": TOOL_NAME},
        "mcp_servers": {
            SERVER_NAME: {
                "command": sys.executable,
                "args": ["-P", "-X", "utf8", "-m", __name__],
                "env": {SESSION_ENV_VAR: str(controls)},
                "enabled": True,
            }
        },
    }


def tui_config(config: dict) -> dict:
    # The remote TUI forwards its whole features table, replacing the app-server's overrides.
    return {
        key: value
        for key, value in config.items()
        if key in ("features", "tui.status_line_provider") or key.startswith("features.")
    }


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: str, color: str) -> dict:
    return {"type": "text", "props": {"color": color}, "children": [value]}


def _router_indicator(
    label: str | None, background: str = "#808080", notes: list[str] | None = None
) -> dict:
    if label == "UNKNOWN":
        return _text("smart router UNKNOWN", "yellow")
    children = [_text("smart router", "primary")]
    if label is not None:
        children.append(
            {
                "type": "row",
                "props": {"gap": 0, "background": background},
                "children": [
                    {
                        "type": "text",
                        "props": {"color": "black", "weight": "bold"},
                        "children": [f" {label} "],
                    }
                ],
            }
        )
    children += [_text(note, "yellow") for note in notes or []]
    return {"type": "row", "props": {"gap": 1}, "children": children}


def _validation_pending(result: dict[str, Any] | None, result_path: Path) -> bool:
    if result is None or result.get("pending") is not True:
        return False
    try:
        age = time.time() - result_path.stat().st_mtime
    except OSError:
        return False
    return age < VALIDATION_PENDING_SECONDS


def _validation_indicator(path: Path, enabled: bool) -> dict:
    result = read_validation_result(path)
    if _validation_pending(result, validation_result_path(path)):
        return _router_indicator(None)
    warning = validation_warning(result)
    shown = None if validation_dismissed_path(path).exists() else warning
    if warning is not None and result is not None and result.get("routing_disabled") is True:
        return _router_indicator(
            "ERROR", "yellow", [*([shown] if shown else []), LOGS_DETAILS_HINT]
        )
    return _router_indicator("ON", "green") if enabled else _router_indicator("OFF")


def render(state: dict[str, Any]) -> dict:
    controls = os.environ.get(SESSION_ENV_VAR)
    enabled = False
    try:
        if not controls:
            raise ValueError("No session controls")
        path = Path(controls)
        enabled = session_routing_enabled(path, retry=False)
        indicator = _validation_indicator(path, enabled)
    except (OSError, UnicodeError, ValueError):
        indicator = _router_indicator("UNKNOWN")

    thread = _object(_object(state).get("thread"))
    model = _object(thread.get("model"))
    model_name = model.get("display_name") or model.get("id") or "Codex"
    auto_active = False
    if controls and enabled:
        try:
            auto_state = json.loads(auto_state_path(Path(controls)).read_text())
            if isinstance(auto_state, dict):
                auto_active = True
                main = model.get("id")
                if main == AUTO_MODEL:
                    main = auto_state.get("model")
                model_name = f"Auto ({main})" if isinstance(main, str) and main else "Auto"
        except (OSError, ValueError):
            pass
    if model.get("reasoning_level") and not auto_active:
        model_name += " " + model["reasoning_level"]
    children = [indicator, _text(model_name, "status-model")]
    cwd = _object(thread.get("workspace")).get("cwd")
    if cwd:
        path = PureWindowsPath(cwd) if "\\" in cwd else PurePosixPath(cwd)
        children.append(_text(path.name or str(path), "status-path"))
    used = _object(_object(thread.get("usage")).get("context")).get("used_percentage")
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
