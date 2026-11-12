"""Routing attribution in Codex's native turn metadata."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ucode.smart_routing.session_env import session_routing_enabled

ROUTING_STATE_KEY = "smart_router_state"
TASK_STATE_SUFFIX = re.compile(r"(?:__sr_(?:on|off))+$")
INTERNAL_REQUEST_PREFIX = "ug-routing-metadata-"
UPDATE_TIMEOUT_SECONDS = 30
CONTROLS_READ_ATTEMPTS = 10
CONTROLS_READ_RETRY_SECONDS = 0.1


def routing_state(enabled: bool) -> str:
    return "on" if enabled else "off"


def stamp_spawn(payload: dict, enabled: bool, output: dict | None = None) -> dict | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("task_name"), str):
        return output
    result = copy.deepcopy(output) if output is not None else {}
    hook = result.setdefault("hookSpecificOutput", {})
    updated = {**tool_input, **hook.get("updatedInput", {})}
    updated["task_name"] = (
        TASK_STATE_SUFFIX.sub("", updated["task_name"]) + f"__sr_{routing_state(enabled)}"
    )
    hook.update(hookEventName="PreToolUse", permissionDecision="allow", updatedInput=updated)
    return result


class Connection(Protocol):
    async def send(self, message: str) -> None: ...


@dataclass
class RootTurn:
    active: str | None = None
    toggle_commands: set[str] = field(default_factory=set)


class RoutingMetadata:
    def __init__(
        self, upstream: Connection, tui: Connection, controls: Path, log: Callable[[str], None]
    ):
        self.upstream = upstream
        self.tui = tui
        self.controls = controls
        self.log = log
        self.roots: dict[str, RootTurn] = {}
        self.children: set[str] = set()
        self.pending: dict[str, asyncio.Future[dict]] = {}
        self.client_requests: set[str | int] = set()
        self.ready = asyncio.Event()
        self.ready.set()
        self.client_idle = asyncio.Event()
        self.client_idle.set()
        self.activity = asyncio.Event()
        self.updating = False
        self.applied: bool | None = None
        self.controls_lock = asyncio.Lock()
        self.controls_ready = asyncio.Event()
        self.controls_ready.set()
        self.failed = False

    async def initialize(self) -> None:
        self.applied = await self.enabled()

    async def enabled(self) -> bool:
        try:
            async with self.controls_lock:
                self.controls_ready.clear()
                attempts_remaining = CONTROLS_READ_ATTEMPTS
                while True:
                    try:
                        enabled = await asyncio.to_thread(session_routing_enabled, self.controls)
                        self.controls_ready.set()
                        return enabled
                    except PermissionError:
                        attempts_remaining -= 1
                        if not attempts_remaining:
                            raise
                        await asyncio.sleep(CONTROLS_READ_RETRY_SECONDS)
        except (OSError, UnicodeError, ValueError) as exc:
            await self.report_failure(exc)
            raise

    async def wait_ready(self) -> None:
        while True:
            await self.ready.wait()
            await self.controls_ready.wait()
            if self.ready.is_set() and self.controls_ready.is_set():
                return

    async def report_failure(self, exc: Exception) -> None:
        self.ready.clear()
        if self.failed:
            return
        self.failed = True
        await self.tui.send(
            json.dumps(
                {
                    "method": "error",
                    "params": {
                        "message": f"Smart Router metadata update failed: {exc}. Check access to session controls at {self.controls}, then restart Codex through ug and submit a new prompt.",
                        "willRetry": False,
                    },
                }
            )
        )

    async def before_tui(self, message: dict) -> dict | None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return message
        tid = params.get("threadId")
        if tid is not None and not isinstance(tid, str):
            return message
        if method == "turn/interrupt":
            if tid in self.roots:
                if self.updating:
                    if "id" in message:
                        await self.tui.send(json.dumps({"id": message["id"], "result": {}}))
                    return None
            return message
        await self.wait_ready()
        identity = message.get("id")
        if method == "initialize" and isinstance(params.get("capabilities", {}), dict):
            params.setdefault("capabilities", {})["experimentalApi"] = True
        if method == "turn/start" and isinstance(tid, str) and tid not in self.children:
            patch = params.get("responsesapiClientMetadataPatch")
            if patch is None or isinstance(patch, dict):
                while True:
                    await self.wait_ready()
                    enabled = await self.enabled()
                    if self.ready.is_set():
                        break
                params["responsesapiClientMetadataPatch"] = {
                    **(patch or {}),
                    ROUTING_STATE_KEY: routing_state(enabled),
                }
        if isinstance(identity, (str, int)) and method in {
            "thread/start",
            "thread/resume",
            "thread/settings/update",
            "turn/start",
            "turn/settings/update",
        }:
            self.client_requests.add(identity)
            self.client_idle.clear()
        return message

    def observe(self, message: dict) -> bool:
        identity = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        result = message.get("result")
        result = result if isinstance(result, dict) else {}
        thread = result.get("thread") or params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            source = thread.get("source")
            is_child = thread.get("parentThreadId") or (
                isinstance(source, dict) and "subAgent" in source
            )
            if is_child:
                self.children.add(thread["id"])
            elif not thread.get("ephemeral"):
                self.roots.setdefault(thread["id"], RootTurn())
        if "method" not in message and isinstance(identity, (str, int)):
            self.client_requests.discard(identity)
            if not self.client_requests:
                self.client_idle.set()
            if isinstance(identity, str) and identity.startswith(INTERNAL_REQUEST_PREFIX):
                future = self.pending.get(identity)
                if future is not None and not future.done():
                    future.set_result(message)
                return True
        tid = params.get("threadId")
        tid = tid if isinstance(tid, str) else None
        root = self.roots.get(tid)
        item = params.get("item")
        if root is not None and isinstance(item, dict) and isinstance(item.get("id"), str):
            command = item.get("command", "")
            if (
                message.get("method") == "item/started"
                and item.get("type") == "commandExecution"
                and isinstance(command, str)
                and "-m ucode.cli" in command
                and any(
                    flag in command
                    for flag in ("--enable-smart-routing", "--disable-smart-routing")
                )
            ):
                root.toggle_commands.add(item["id"])
            elif message.get("method") == "item/completed":
                root.toggle_commands.discard(item["id"])
                self.activity.set()
        turn = params.get("turn")
        if root is not None and isinstance(turn, dict) and isinstance(turn.get("id"), str):
            if message.get("method") == "turn/started":
                root.active = turn["id"]
            elif message.get("method") == "turn/completed" and root.active == turn["id"]:
                root.active = None
                self.activity.set()
        if message.get("method") == "thread/closed":
            self.roots.pop(tid, None)
        return False

    async def rpc(self, method: str, params: dict) -> dict:
        identity = f"{INTERNAL_REQUEST_PREFIX}{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.upstream.send(
                json.dumps({"id": identity, "method": method, "params": params})
            )
            response = await asyncio.wait_for(future, timeout=10)
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"{method}: expected an object result")
            return result
        finally:
            self.pending.pop(identity, None)

    async def refresh(self) -> None:
        self.ready.clear()
        self.updating = True
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT_SECONDS):
                await self.client_idle.wait()
                roots = list(self.roots.items())
                for tid, root in roots:
                    while root.toggle_commands:
                        self.activity.clear()
                        await self.activity.wait()
                    turn_id = root.active
                    if turn_id is not None:
                        try:
                            await self.rpc("turn/interrupt", {"threadId": tid, "turnId": turn_id})
                        except RuntimeError:
                            if root.active == turn_id:
                                raise
                        while root.active == turn_id:
                            self.activity.clear()
                            await self.activity.wait()
                enabled = await self.enabled()
                self.applied = enabled
                self.log(
                    f"[METADATA] main turns updated: state={routing_state(enabled)} roots={len(roots)}"
                )
            self.ready.set()
        finally:
            self.updating = False

    async def watch(self) -> None:
        try:
            while True:
                if await self.enabled() != self.applied:
                    await self.refresh()
                await asyncio.sleep(0.1)
        except (OSError, UnicodeError, ValueError, RuntimeError, TimeoutError) as exc:
            await self.report_failure(exc)
            raise
