"""Cowork plugin hooks that keep Claude informed about Unity Gateway connectors.

Cowork connects plugin MCP servers lazily and drops them when a session sits idle,
then tells Claude they disconnected. These hooks cannot make Cowork reconnect
sooner, but they refresh the Databricks token every proxy mints, wake each upstream
MCP server, and tell Claude which connectors answered. They run on SessionStart and
on UserPromptSubmit after an idle gap.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ucode.databricks import ensure_pat_bearer, get_databricks_token
from ucode.mcp_proxy import _httpx
from ucode.ug_headers import ug_headers

SESSION_START_PROBE_TIMEOUT_SECONDS = 15.0
# UserPromptSubmit blocks the prompt and has a 30-second default timeout.
PROMPT_PROBE_TIMEOUT_SECONDS = 10.0
# Cowork was observed dropping idle connectors after about five minutes.
IDLE_GAP_SECONDS = 180.0
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def hook_output(
    payload: object,
    servers: dict[str, str],
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool = False,
    now: datetime | None = None,
) -> dict:
    if not isinstance(payload, dict) or not servers:
        return {}
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        ready = _warm(
            servers,
            workspace,
            profile,
            use_pat=use_pat,
            timeout=SESSION_START_PROBE_TIMEOUT_SECONDS,
        )
        context = session_start_context(list(servers), ready)
    elif event == "UserPromptSubmit":
        last_reply = _last_assistant_time(payload.get("transcript_path"))
        current = now or datetime.now(UTC)
        if last_reply is None or (current - last_reply).total_seconds() < IDLE_GAP_SECONDS:
            return {}
        ready = _warm(
            servers, workspace, profile, use_pat=use_pat, timeout=PROMPT_PROBE_TIMEOUT_SECONDS
        )
        context = idle_resume_context(list(servers), ready, current)
    else:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def session_start_context(names: list[str], ready: set[str]) -> str:
    context = (
        f"Unity Gateway connectors configured for this session: {_quoted(names)}. Use them for "
        "requests about the user's own resources on those services. Their tools may still "
        "be loading; before saying a connector is unavailable, follow the "
        "`unity-gateway-connectors` skill."
    )
    loading = [name for name in names if name not in ready]
    if loading:
        context += (
            f" These did not answer at session start and may still be starting: {_quoted(loading)}."
        )
    return context


def idle_resume_context(names: list[str], ready: set[str], checked_at: datetime) -> str:
    context = (
        'After an idle break, Cowork may show stale "MCP servers disconnected" notices for '
        "Unity Gateway connectors. Do not say a connector is unavailable based on an earlier "
        "notice; check current tools and follow the `unity-gateway-connectors` skill."
    )
    answered = [name for name in names if name in ready]
    silent = [name for name in names if name not in ready]
    stamp = checked_at.strftime("%H:%M UTC")
    if answered:
        context += (
            f" At {stamp} these connectors answered: {_quoted(answered)}. Their tools may take "
            "a few seconds to reappear."
        )
    if silent and answered:
        context += f" These did not answer: {_quoted(silent)}."
    return context


def _quoted(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _last_assistant_time(transcript_path: object) -> datetime | None:
    # The current prompt may already be recorded, so measure from Claude's last reply.
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with Path(transcript_path).open("rb") as transcript:
            transcript.seek(0, 2)
            transcript.seek(max(0, transcript.tell() - TRANSCRIPT_TAIL_BYTES))
            lines = transcript.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        try:
            stamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return None


def _warm(
    servers: dict[str, str],
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool,
    timeout: float,
) -> set[str]:
    if use_pat and not ensure_pat_bearer(profile):
        return set()
    try:
        # One refresh here saves every proxy from racing to refresh the same token.
        token = get_databricks_token(workspace, profile)
    except RuntimeError:
        return set()
    return anyio.run(_probe_all, servers, token, timeout)


async def _probe_all(servers: dict[str, str], token: str, timeout: float) -> set[str]:
    ready: set[str] = set()

    async def probe(name: str, url: str) -> None:
        try:
            await _probe(url, token, timeout)
        except Exception:  # noqa: BLE001 - any failure just means "not ready yet"
            return
        ready.add(name)

    with anyio.move_on_after(timeout):
        async with anyio.create_task_group() as tg:
            for name, url in servers.items():
                tg.start_soon(probe, name, url)
    return ready


async def _probe(url: str, token: str, timeout: float) -> None:
    httpx = _httpx()
    headers = {**ug_headers(), "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                await session.list_tools()
