"""Cursor agent: registers Databricks MCP servers in ~/.cursor/mcp.json.

Cursor is an MCP-only integration. `cursor-agent` runs models on the user's own
Cursor account and exposes no gateway base URL, so ucode configures no models
for it (it is an `McpClient`, not an `Agent`, so it stays out of `agents.AGENTS`). What ucode does is register
Databricks MCP servers in Cursor's config, using the same uniform mechanism as
every other client: a local **stdio** server that runs `ug mcp-proxy`, which
bridges to the Databricks MCP endpoint and mints a fresh OAuth token per request
(see `ucode.mcp_proxy`). So Cursor needs no token in its config and no launch-
time token export — `cursor-agent` just spawns the proxy like any stdio server.

`cursor-agent` reads `~/.cursor/mcp.json` directly, so entries are merged into
that shared file (preserving anything already there) and removed surgically,
mirroring how Claude/Codex edit their shared config rather than restoring a
whole-file backup.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ucode.config_io import apply_json_mcp_diff, read_json_safe, write_json_file
from ucode.constants import MCP_USER_SCOPE
from ucode.launcher import exec_or_spawn
from ucode.mcp_oauth import CURSOR_OAUTH_CLIENT_ID

from .interface import McpServer

CURSOR_BINARY = "cursor-agent"
CURSOR_CONFIG_DIR = Path.home() / ".cursor"
CURSOR_MCP_CONFIG_PATH = CURSOR_CONFIG_DIR / "mcp.json"


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
    # Cursor's remote-MCP-with-OAuth schema: a `url` server plus an `auth` object
    # naming a pre-registered OAuth client. Cursor drives the OAuth itself (to its
    # fixed `http://localhost:8787/callback` redirect) instead of the stdio proxy,
    # so the user gets Cursor's native connection login. Scopes are omitted — the
    # MCP protected-resource metadata advertises them, and Cursor discovers them
    # (the same way Claude Code's `--transport http --client-id` flow does).
    return {
        "url": url,
        "auth": {"CLIENT_ID": client_id},
    }


def write_http_mcp_server_config(name: str, url: str, client_id: str) -> bool:
    """Add (or replace) an **OAuth HTTP** MCP server (url + pre-registered client) in
    ~/.cursor/mcp.json, so Cursor drives the connection login itself instead of the
    token-injecting stdio proxy."""
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

    No token wiring here: the Databricks MCP servers in ~/.cursor/mcp.json run
    `ug mcp-proxy`, which authenticates itself, so `ug cursor` is a thin
    convenience wrapper over `cursor-agent` (kept for symmetry with the other
    `ucode <agent>` launchers)."""
    exec_or_spawn([CURSOR_BINARY, *tool_args])


def _user_scope(removed: bool) -> list[str]:
    """``add``/``remove``'s return shape: Cursor has only one scope, where ug writes."""
    return [MCP_USER_SCOPE] if removed else []


class CursorMcpClient:
    """Registers MCP servers in Cursor's ``~/.cursor/mcp.json``.

    Cursor can drive OAuth itself against a published app (a ``url`` entry with an ``auth`` block,
    so it shows its own connection login), which is a much better experience than the stdio
    ``ug mcp-proxy`` for a connection-backed service. ug core decides *what* to register (an
    :class:`McpServer`, with the proxy-vs-native-OAuth choice already made); this decides *how*.
    """

    display: str = "Cursor"
    binary: str = CURSOR_BINARY
    # The published OAuth app a native HTTP+OAuth entry registers against. It must be published on
    # the workspace, with its loopback `/callback` redirect registered on `/oidc` (which lacks
    # dynamic client registration), so ug core checks that per workspace (`oauth_client_available`).
    oauth_client_id: str | None = CURSOR_OAUTH_CLIENT_ID

    def add(self, name: str, server: McpServer) -> list[str]:
        if server.oauth_client is not None:
            return _user_scope(
                write_http_mcp_server_config(name, server.url, client_id=server.oauth_client)
            )
        return _user_scope(write_mcp_server_config(name, list(server.proxy_argv)))

    def remove(self, name: str) -> list[str]:
        return _user_scope(remove_mcp_server_config(name))

    def apply(self, add: Mapping[str, McpServer], remove: set[str]) -> set[str]:
        """Collapse a whole diff into ONE read-modify-write of ``mcp.json``, instead of a config
        write per server (see ``mcp.apply_mcp_server_changes``)."""
        entries = {name: self.entry(server) for name, server in add.items()}
        return write_user_mcp_servers(entries, remove) or set()

    def entry(self, server: McpServer) -> dict:
        """The on-disk entry :meth:`add` would have written for ``server``."""
        if server.oauth_client is not None:
            return build_http_mcp_server_entry(server.url, server.oauth_client)
        return build_mcp_server_entry(list(server.proxy_argv))

    def live_status(self) -> dict[str, str]:
        # Imported lazily: `ucode.mcp` imports this package. The shared `mcp list` reader and
        # health-line parser live in mcp.py, where the other clients use them too.
        from ucode import mcp

        output = mcp._read_mcp_listing([self.binary, "mcp", "list"])
        if output is None:
            return {}
        # Parse the rows first and read the output as "no servers configured" only when none
        # parsed: that check matches phrases ("not found") also present in one server's failure.
        parsed = mcp._parse_health_mcp_list(output)
        if not parsed and mcp._is_missing_mcp_server_output(output):
            return {}
        return parsed


MCP_CLIENT = CursorMcpClient()
