"""Routing attribution in Codex's native turn metadata."""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ucode.constants import AGENT_CODEX
from ucode.smart_routing import session_log
from ucode.smart_routing.session_env import (
    read_unsupported_validation,
    read_validation_result,
    session_routing_enabled,
    validation_dismissed_path,
    validation_warning,
)

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
        self.recorded_validations: set[str] = set()
        self.arming_threads: set[str] = set()
        self.armed_turns: set[tuple[str, str]] = set()

    # Only a prompt sent while the warning shows hides it, so a turn already running when the result arrives does not.
    def arm_validation_warning(self, thread_id: str) -> None:
        if validation_warning(read_validation_result(self.controls)) is None:
            return
        if not validation_dismissed_path(self.controls).exists():
            self.arming_threads.add(thread_id)

    # The root thread id first appears on a prompt; startup validation may still be running then.
    def record_route_validation(self, thread_id: str) -> None:
        if thread_id in self.recorded_validations:
            return
        result = read_unsupported_validation(self.controls)
        if result is None:
            return
        self.recorded_validations.add(thread_id)
        try:
            session_log.append_route_validation(AGENT_CODEX, thread_id, result)
        except (OSError, ValueError) as exc:
            self.log(f"[METADATA] Cannot record route validation for thread={thread_id}: {exc}")

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
            await asyncio.to_thread(self.record_route_validation, tid)
            await asyncio.to_thread(self.arm_validation_warning, tid)
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
        if message.get("method") in ("turn/started", "turn/completed"):
            self._observe_turn(message["method"], params)
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

    def _observe_turn(self, method: str, params: dict) -> None:
        thread_id, turn = params.get("threadId"), params.get("turn")
        if not isinstance(thread_id, str) or not isinstance(turn, dict):
            return
        if not isinstance(turn.get("id"), str):
            return
        key = (thread_id, turn["id"])
        if method == "turn/started" and thread_id in self.arming_threads:
            self.arming_threads.discard(thread_id)
            self.armed_turns.add(key)
        elif method == "turn/completed" and key in self.armed_turns:
            self.armed_turns.discard(key)
            with suppress(OSError):
                validation_dismissed_path(self.controls).touch()
