"""Cursor agent: registers Databricks MCP servers in ~/.cursor/mcp.json.

Cursor is an MCP-only integration. `cursor-agent` runs models on the user's own
Cursor account and exposes no gateway base URL, so ucode configures no models
for it (it stays out of `agents.__init__._MODULES`). What ucode does is register
Databricks MCP servers in Cursor's config. Connection-backed services use
Cursor's native HTTP OAuth with its published CIMD client. Other services use
`ug mcp-proxy`, which refreshes the workspace token. Neither entry stores a bearer.

`cursor-agent` reads `~/.cursor/mcp.json` directly, so entries are merged into
that shared file (preserving anything already there) and removed surgically,
mirroring how Claude/Codex edit their shared config rather than restoring a
whole-file backup.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ucode.config_io import apply_json_mcp_diff, read_json_safe, write_json_file
from ucode.launcher import exec_or_spawn
from ucode.os_compatibility import subprocess_cross_os

CURSOR_BINARY = "cursor-agent"
CURSOR_CONFIG_DIR = Path.home() / ".cursor"
CURSOR_MCP_CONFIG_PATH = CURSOR_CONFIG_DIR / "mcp.json"
MINIMUM_CIMD_VERSION = (2026, 10, 22)


def supports_cimd() -> bool:
    """Older Cursor builds ignore auth.CLIENT_ID and attempt unsupported DCR."""
    try:
        result = subprocess_cross_os.run(
            [CURSOR_BINARY, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    match = re.search(r"\b(\d{4})\.(\d{2})\.(\d{2})\b", result.stdout or "")
    return bool(match and tuple(map(int, match.groups())) >= MINIMUM_CIMD_VERSION)


def build_mcp_server_entry(argv: list[str]) -> dict:
    # Cursor's stdio MCP schema: `command` + `args`. ucode registers the
    # `ug mcp-proxy ...` bridge here so the proxy handles auth/refresh.
    return {
        "command": argv[0],
        "args": list(argv[1:]),
    }


def _upsert_mcp_server(name: str, entry: dict) -> bool:
    """Add (or replace) one entry in ~/.cursor/mcp.json's `mcpServers`, merging so
    unrelated entries the user already configured survive. Returns True when an
    entry with this name was already present (i.e. this was a replacement)."""
    existing = read_json_safe(CURSOR_MCP_CONFIG_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    removed = name in mcp_servers
    mcp_servers[name] = entry
    existing["mcpServers"] = mcp_servers
    write_json_file(CURSOR_MCP_CONFIG_PATH, existing)
    return removed


def write_user_mcp_servers(add: dict[str, dict], remove: set[str]) -> set[str]:
    """Apply ``add``/``remove`` to Cursor's `mcpServers` in a single read-modify-write. Returns the names actually removed."""
    return apply_json_mcp_diff(CURSOR_MCP_CONFIG_PATH, "mcpServers", add, remove)


def write_mcp_server_config(name: str, argv: list[str]) -> bool:
    """Add (or replace) a stdio (`ug mcp-proxy`) MCP server in ~/.cursor/mcp.json."""
    return _upsert_mcp_server(name, build_mcp_server_entry(argv))


def build_http_mcp_server_entry(url: str, client_id: str) -> dict:
    """Let Cursor drive the MCP OAuth login with its published CIMD client."""
    return {"url": url, "auth": {"CLIENT_ID": client_id}}


def write_http_mcp_server_config(name: str, url: str, client_id: str) -> bool:
    return _upsert_mcp_server(name, build_http_mcp_server_entry(url, client_id))


def remove_mcp_server_config(name: str) -> bool:
    """Surgically remove one MCP server entry from ~/.cursor/mcp.json.

    Returns True when an entry was removed, False when it wasn't present."""
    existing = read_json_safe(CURSOR_MCP_CONFIG_PATH)
    mcp_servers = existing.get("mcpServers")
    if not isinstance(mcp_servers, dict) or name not in mcp_servers:
        return False
    mcp_servers.pop(name)
    existing["mcpServers"] = mcp_servers
    write_json_file(CURSOR_MCP_CONFIG_PATH, existing)
    return True


def launch(state: dict, tool_args: list[str]) -> None:
    """Hand the terminal to `cursor-agent`.

    MCP authentication is configured in ~/.cursor/mcp.json, so `ug cursor` is a
    thin convenience wrapper over `cursor-agent`."""
    exec_or_spawn([CURSOR_BINARY, *tool_args])
