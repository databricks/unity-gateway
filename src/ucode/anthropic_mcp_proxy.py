"""Transparent stdio bridge to an authenticated Anthropic account connector."""

from __future__ import annotations

import sys
import uuid
from urllib.parse import quote

import anyio
from anyio import to_thread
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from ucode.agents.desktop.anthropic_cowork import AccountClient
from ucode.desktop_auth import ClaudeDesktopAuthError, get_claude_subscription_token
from ucode.mcp_proxy import _httpx


def token_auth(claude_binary: str):
    httpx = _httpx()

    class SubscriptionAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            token = await to_thread.run_sync(get_claude_subscription_token, claude_binary)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request

    return SubscriptionAuth()


async def _pump(source, destination, upstream: bool = False) -> None:
    async with source, destination:
        async for message in source:
            if isinstance(message, Exception):
                raise RuntimeError("Anthropic MCP transport failed.") from None
            await destination.send(message)
    if upstream:
        raise RuntimeError("Anthropic MCP transport closed unexpectedly.")


async def _run(server_id: str, claude_binary: str) -> None:
    httpx = _httpx()
    url = "https://mcp-proxy.anthropic.com/v1/mcp/" + quote(server_id, safe="")
    async with httpx.AsyncClient(
        auth=token_auth(claude_binary),
        follow_redirects=False,
        headers={
            "User-Agent": "unity-gateway-cowork",
            "Accept-Encoding": "identity",
            "X-Mcp-Client-Session-Id": str(uuid.uuid4()),
        },
        timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30),
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with stdio_server() as (stdio_read, stdio_write):
                async with anyio.create_task_group() as group:
                    group.start_soon(_pump, streams[0], stdio_write, True)
                    await _pump(stdio_read, streams[1])
                    group.cancel_scope.cancel()


def serve(server_id: str, claude_binary: str) -> None:
    try:
        with AccountClient(claude_binary) as client:
            if server_id not in client.connectors().values():
                raise RuntimeError(
                    "Anthropic connector is no longer eligible. Run `ug configure` again."
                )
        anyio.run(_run, server_id, claude_binary)
    except ClaudeDesktopAuthError as exc:
        print(f"ug anthropic-mcp-proxy: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print(
            "ug anthropic-mcp-proxy: connector unavailable. Check Claude sign-in, "
            "run `ug configure`, then start a new Cowork task.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
