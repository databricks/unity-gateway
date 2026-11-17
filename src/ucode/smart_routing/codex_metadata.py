"""Routing attribution in Codex's native turn metadata."""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Callable
from pathlib import Path

from ucode.smart_routing.session_env import session_routing_enabled

ROUTING_STATE_KEY = "smart_router_state"
TASK_STATE_SUFFIX = re.compile(r"(?:__sr_(?:on|off))+$")


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


class RoutingMetadata:
    def __init__(self, controls: Path, log: Callable[[str], None]):
        self.controls = controls
        self.log = log
        self.children: set[str] = set()

    async def enabled(self) -> bool | None:
        try:
            return await asyncio.to_thread(session_routing_enabled, self.controls)
        except (OSError, UnicodeError, ValueError) as exc:
            self.log(
                f"[METADATA] Cannot read session controls at {self.controls}: {exc}; "
                "forwarding without a routing metadata update"
            )
            return None

    async def before_tui(self, message: dict) -> dict:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return message
        if method == "turn/interrupt":
            return message
        if method == "initialize" and isinstance(params.get("capabilities", {}), dict):
            params.setdefault("capabilities", {})["experimentalApi"] = True
        tid = params.get("threadId")
        if method == "turn/start" and isinstance(tid, str) and tid not in self.children:
            patch = params.get("responsesapiClientMetadataPatch")
            if patch is None or isinstance(patch, dict):
                enabled = await self.enabled()
                if enabled is None:
                    return message
                params["responsesapiClientMetadataPatch"] = {
                    **(patch or {}),
                    ROUTING_STATE_KEY: routing_state(enabled),
                }
                self.log(f"[METADATA] main turn: state={routing_state(enabled)} thread={tid}")
        return message

    def observe(self, message: dict) -> None:
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
