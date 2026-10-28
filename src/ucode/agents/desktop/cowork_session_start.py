"""Warm Unity Gateway connectors when a Cowork session starts.

Cowork connects plugin MCP servers lazily, so their tools can be missing on the
first turn. This hook cannot make Cowork connect sooner, but it can refresh the
Databricks token every proxy will mint, wake each upstream MCP server, and tell
Claude which connectors exist and which ones answered.
"""

from __future__ import annotations

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ucode.databricks import ensure_pat_bearer, get_databricks_token
from ucode.mcp_proxy import _httpx
from ucode.ug_headers import ug_headers

PROBE_TIMEOUT_SECONDS = 15.0


def session_start_output(
    payload: object,
    servers: dict[str, str],
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool = False,
) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return {}
    if not servers:
        return {}
    ready = _warm(servers, workspace, profile, use_pat=use_pat)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": connector_context(list(servers), ready),
        }
    }


def connector_context(names: list[str], ready: set[str]) -> str:
    listed = ", ".join(f"`{name}`" for name in names)
    loading = [name for name in names if name not in ready]
    context = (
        f"Unity Gateway connectors configured for this session: {listed}. Use them for "
        "requests about the user's own resources on those services. Their tools may still "
        "be loading; before saying a connector is unavailable, follow the "
        "`unity-gateway-connectors` skill."
    )
    if loading:
        context += (
            " These did not answer at session start and may still be starting: "
            + ", ".join(f"`{name}`" for name in loading)
            + "."
        )
    return context


def _warm(
    servers: dict[str, str], workspace: str, profile: str | None, *, use_pat: bool
) -> set[str]:
    if use_pat and not ensure_pat_bearer(profile):
        return set()
    try:
        # One refresh here saves every proxy from racing to refresh the same token.
        token = get_databricks_token(workspace, profile)
    except RuntimeError:
        return set()
    return anyio.run(_probe_all, servers, token)


async def _probe_all(servers: dict[str, str], token: str) -> set[str]:
    ready: set[str] = set()

    async def probe(name: str, url: str) -> None:
        try:
            await _probe(url, token)
        except Exception:  # noqa: BLE001 - any failure just means "not ready yet"
            return
        ready.add(name)

    with anyio.move_on_after(PROBE_TIMEOUT_SECONDS):
        async with anyio.create_task_group() as tg:
            for name, url in servers.items():
                tg.start_soon(probe, name, url)
    return ready


async def _probe(url: str, token: str) -> None:
    httpx = _httpx()
    headers = {**ug_headers(), "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=PROBE_TIMEOUT_SECONDS) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                await session.list_tools()
