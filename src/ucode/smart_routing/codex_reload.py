"""Opt-in controller for reloading Codex after a session routing toggle."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ucode.constants import (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.smart_routing.session_env import _read


def session_routing_enabled(controls: Path) -> bool:
    overrides = _read(controls)
    return not all(
        overrides.get(key) == "0"
        for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    )


class RoutingReload:
    def __init__(self, upstream, tui, config: dict, controls: Path, log: Callable[[str], None]):
        self.upstream = upstream
        self.tui = tui
        self.config = copy.deepcopy(config)
        self.controls = controls
        self.log = log
        self.root: str | None = None
        self.parents: dict[str, str | None] = {}
        self.task_paths: dict[str, str] = {}
        self.active: dict[str, str] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.thread_requests: dict[str | int, dict] = {}
        self.turn_settings: dict = {}
        self.resume_settings: dict = {}
        self.permission_config: dict = {}
        self.last_turn = ""
        self.ready = asyncio.Event()
        self.ready.set()
        self.reloading = False
        self.applied = self.enabled()

    def enabled(self) -> bool:
        return session_routing_enabled(self.controls)

    async def before_tui(self, message: dict) -> None:
        if "method" not in message:
            return
        await self.ready.wait()
        method = message["method"]
        params = message.get("params", {})
        if method in {"thread/start", "thread/resume"} and "id" in message:
            self.thread_requests[message["id"]] = copy.deepcopy(params)
        if method == "thread/settings/update" and params.get("threadId") == self.root:
            self.remember_settings(params)
        if method == "turn/start" and params.get("threadId") == self.root:
            self.remember_settings(params)
            self.turn_settings.update(
                {
                    key: params[key]
                    for key in (
                        "model",
                        "effort",
                        "collaborationMode",
                        "serviceTier",
                        "multiAgentMode",
                        "approvalPolicy",
                        "approvalsReviewer",
                        "cwd",
                        "runtimeWorkspaceRoots",
                        "permissions",
                        "sandboxPolicy",
                    )
                    if params.get(key) is not None
                }
            )

    def remember_settings(self, source: dict) -> None:
        for key in (
            "model",
            "modelProvider",
            "serviceTier",
            "approvalPolicy",
            "approvalsReviewer",
            "cwd",
            "runtimeWorkspaceRoots",
            "permissions",
        ):
            if source.get(key) is not None:
                self.resume_settings[key] = copy.deepcopy(source[key])
        policy = source.get("sandboxPolicy") or source.get("sandbox")
        if policy is not None:
            self.resume_settings.pop("permissions", None)
            self.turn_settings.pop("permissions", None)
        if isinstance(policy, str):
            self.resume_settings["sandbox"] = policy
        elif isinstance(policy, dict):
            modes = {
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "dangerFullAccess": "danger-full-access",
            }
            mode = modes.get(policy.get("type"))
            if mode is None:
                raise RuntimeError("This POC cannot preserve the current sandbox profile")
            self.resume_settings["sandbox"] = mode
            self.turn_settings["sandboxPolicy"] = copy.deepcopy(policy)
            if mode == "workspace-write":
                self.permission_config["sandbox_workspace_write"] = {
                    target: copy.deepcopy(policy[key])
                    for key, target in {
                        "writableRoots": "writable_roots",
                        "networkAccess": "network_access",
                        "excludeTmpdirEnvVar": "exclude_tmpdir_env_var",
                        "excludeSlashTmp": "exclude_slash_tmp",
                    }.items()
                    if key in policy
                }
        profile = source.get("activePermissionProfile")
        if isinstance(profile, dict) and profile.get("id"):
            self.resume_settings["permissions"] = profile["id"]
            self.turn_settings["permissions"] = profile["id"]
        if "permissions" in self.resume_settings:
            self.resume_settings.pop("sandbox", None)
            self.turn_settings.pop("sandboxPolicy", None)

    def observe(self, message: dict) -> bool:
        identity = message.get("id")
        result = message.get("result") or {}
        params = message.get("params") or {}
        thread = result.get("thread") or params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            tid = thread["id"]
            self.remember_thread(thread)
            if identity in self.thread_requests:
                requested = self.thread_requests.pop(identity)
                if not thread.get("ephemeral") and not requested.get("ephemeral"):
                    self.root = tid
                    self.remember_settings(requested)
                    self.remember_settings(result)
        method = message.get("method")
        tid = params.get("threadId")
        if method == "thread/settings/updated" and tid == self.root:
            self.remember_settings(params.get("threadSettings") or {})
        if method == "turn/started" and isinstance(tid, str):
            self.active[tid] = params["turn"]["id"]
            if tid == self.root:
                self.last_turn = params["turn"]["id"]
        elif method == "turn/completed" and isinstance(tid, str):
            if self.active.get(tid) == params["turn"]["id"]:
                self.active.pop(tid)
        if identity in self.pending and "method" not in message:
            future = self.pending[identity]
            if not future.done():
                future.set_result(message)
            return True
        event_thread = tid or (thread.get("id") if isinstance(thread, dict) else None)
        return (
            self.reloading
            and method in {"thread/closed", "thread/started"}
            and isinstance(event_thread, str)
            and self.belongs_to_root(event_thread)
        )

    def remember_thread(self, thread: dict) -> None:
        tid = thread["id"]
        source = thread.get("source")
        subagent = source.get("subAgent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn", {}) if isinstance(subagent, dict) else {}
        self.parents[tid] = thread.get("parentThreadId") or spawn.get("parent_thread_id")
        if spawn.get("agent_path"):
            self.task_paths[tid] = spawn["agent_path"]

    async def rpc(self, method: str, params: dict, timeout: float = 10) -> dict:
        identity = f"ug-reload-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.upstream.send(
                json.dumps({"id": identity, "method": method, "params": params})
            )
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RuntimeError(f"{method}: {response['error'].get('message', 'failed')}")
            return response.get("result", {})
        finally:
            self.pending.pop(identity, None)

    def belongs_to_root(self, thread: str) -> bool:
        seen = set()
        while thread and thread not in seen:
            if thread == self.root:
                return True
            seen.add(thread)
            thread = self.parents.get(thread) or ""
        return False

    async def loaded_tree(self) -> list[str]:
        loaded = []
        cursor = None
        while True:
            page = await self.rpc("thread/loaded/list", {"cursor": cursor, "limit": 100})
            loaded.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        for tid in loaded:
            if tid not in self.parents:
                thread = (await self.rpc("thread/read", {"threadId": tid}))["thread"]
                self.remember_thread(thread)
        return [tid for tid in loaded if self.belongs_to_root(tid)]

    async def reload(self) -> None:
        self.ready.clear()
        self.reloading = True
        started = time.monotonic()
        root = self.root
        interrupted_children = set()
        self.log(f"[RELOAD] interrupting root={root} controls={self.controls}")
        deadline = started + 20
        while True:
            tree = await self.loaded_tree()
            if not tree:
                break
            active = [(tid, turn) for tid, turn in self.active.items() if tid in tree]
            active.sort(key=lambda pair: pair[0] != root)
            for tid, turn in active:
                if tid != root:
                    interrupted_children.add(self.task_paths.get(tid, tid))
                try:
                    await self.rpc("turn/interrupt", {"threadId": tid, "turnId": turn})
                except RuntimeError:
                    if self.active.get(tid) == turn:
                        raise
            if not any(tid in self.active for tid in tree):
                for tid in sorted(tree, key=lambda tid: tid == root):
                    await self.rpc("thread/unsubscribe", {"threadId": tid})
            if time.monotonic() >= deadline:
                raise TimeoutError("Conversation did not unload within 20 seconds")
            await asyncio.sleep(0.05)

        enabled = self.enabled()
        config = copy.deepcopy(self.config)
        config.update(copy.deepcopy(self.permission_config))
        provider = config["model_provider"]
        headers = config["model_providers"][provider].setdefault("http_headers", {})
        recipe = headers[SMART_ROUTER_RECIPE_HEADER] if enabled else "DISABLED"
        headers[SMART_ROUTER_RECIPE_HEADER] = recipe
        config.setdefault("responses_api_metadata", {})["smart_router_recipe_name"] = recipe
        await self.rpc(
            "thread/resume",
            {**self.resume_settings, "threadId": root, "config": config, "excludeTurns": True},
        )
        self.applied = enabled
        self.reloading = False
        self.log(
            f"[RELOAD] resumed root={root} recipe={recipe} elapsed={time.monotonic() - started:.3f}s"
        )
        state = "on" if enabled else "off"
        prompt = (
            f"Smart Router is now {state}. The controller reloaded this conversation and its native "
            "subagents to update request attribution. Do not repeat the routing toggle command. "
            "Continue the previous unfinished task, using followup_task for existing children. "
            "Running commands were interrupted; inspect any completed effects before retrying them. "
            f"Existing interrupted agents: {json.dumps(sorted(interrupted_children))}. "
            "Resume those existing assignments with followup_task; use list_agents to resolve any thread IDs. "
            "Do not spawn replacement agents. If there are no interrupted children and the previous task "
            "was only changing Smart Router, confirm the new state and stop."
        )
        await self.rpc(
            "turn/start",
            {
                **self.turn_settings,
                "threadId": root,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        self.ready.set()

    async def watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                if self.root and self.enabled() != self.applied:
                    await self.reload()
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            self.ready.clear()
            self.log(f"[RELOAD FAILED] root={self.root} controls={self.controls} error={exc}")
            await self.tui.send(
                json.dumps(
                    {
                        "method": "error",
                        "params": {
                            "threadId": self.root,
                            "turnId": self.last_turn,
                            "willRetry": False,
                            "error": {
                                "message": f"Smart Router reload failed: {exc}. Saved conversation: {self.root}. Restart this POC with --session-controls {self.controls} -- resume {self.root}.",
                                "codexErrorInfo": None,
                                "additionalDetails": None,
                            },
                        },
                    }
                )
            )
            raise
