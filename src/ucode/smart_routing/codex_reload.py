"""Reload native Codex conversations when their session routing state changes."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ucode.config_io import deep_merge_dict
from ucode.constants import (
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
    SMART_ROUTER_RECIPE_HEADER,
)
from ucode.smart_routing.session_env import _read

RELOAD_TIMEOUT_SECONDS = 30
RPC_TIMEOUT_SECONDS = 10
INTERNAL_REQUEST_PREFIX = "ug-reload-"


class Connection(Protocol):
    async def send(self, message: str) -> None: ...


@dataclass
class ConversationSettings:
    resume: dict = field(default_factory=dict)
    turn: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)


def routing_config(config: dict, enabled: bool) -> dict:
    result = copy.deepcopy(config)
    headers = result["model_providers"][result["model_provider"]].setdefault("http_headers", {})
    recipe = (
        next(
            (
                value
                for name, value in headers.items()
                if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold()
            ),
            None,
        )
        if enabled
        else "DISABLED"
    )
    if not isinstance(recipe, str) or not recipe.strip():
        raise RuntimeError("Codex smart routing requires a nonempty launch recipe")
    for name in list(headers):
        if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold():
            del headers[name]
    headers[SMART_ROUTER_RECIPE_HEADER] = recipe
    result.setdefault("responses_api_metadata", {})["smart_router_recipe_name"] = recipe
    return result


def session_routing_enabled(controls: Path) -> bool:
    overrides = _read(controls)
    return not all(
        overrides.get(key) == "0"
        for key in (ENABLE_SMART_ROUTING_ENV_VAR, ENABLE_SUBAGENT_ROUTING_ENV_VAR)
    )


class RoutingReload:
    def __init__(
        self,
        upstream: Connection,
        tui: Connection,
        config: dict,
        controls: Path,
        log: Callable[[str], None],
    ):
        self.upstream = upstream
        self.tui = tui
        self.config = copy.deepcopy(config)
        self.controls = controls
        self.log = log
        self.root: str | None = None
        self.parents: dict[str, str | None] = {}
        self.task_paths: dict[str, str] = {}
        self.active: dict[str, str] = {}
        self.pending: dict[str, asyncio.Future[dict]] = {}
        self.client_requests: dict[str | int, tuple[str, dict]] = {}
        self.client_idle = asyncio.Event()
        self.client_idle.set()
        self.settings = ConversationSettings()
        self.conversations: dict[str, ConversationSettings] = {}
        self.last_turn = ""
        self.ready = asyncio.Event()
        self.ready.set()
        self.reloading = False
        self.reload_requested = False
        self.cancel_continuation = False
        self.buffered_turns: set[str] = set()
        self.continuations: dict[str, str] = {}
        self.applied = self.enabled()

    @property
    def turn_settings(self) -> dict:
        return self.settings.turn

    @property
    def resume_settings(self) -> dict:
        return self.settings.resume

    @property
    def permission_config(self) -> dict:
        return self.settings.permissions

    def enabled(self) -> bool:
        return session_routing_enabled(self.controls)

    def buffer_input(self, message: dict) -> None:
        params = message.get("params") or {}
        if (
            not self.ready.is_set()
            and message.get("method") == "turn/start"
            and isinstance(params.get("threadId"), str)
        ):
            self.buffered_turns.add(params["threadId"])

    async def before_tui(self, message: dict) -> dict | None:
        if "method" not in message:
            return message
        if message["method"] == "turn/interrupt" and self.reloading:
            self.cancel_continuation = True
            if "id" in message:
                await self.tui.send(json.dumps({"id": message["id"], "result": {}}))
            return None
        await self.ready.wait()
        method = message["method"]
        params = message.setdefault("params", {})
        if method == "turn/start":
            tid = params.get("threadId")
            self.buffered_turns.discard(tid)
            continuation = self.continuations.pop(tid, None)
            if continuation:
                params["input"] = [{"type": "text", "text": continuation}, *params.get("input", [])]
        if method in {"thread/start", "thread/resume"}:
            # New roots and resumed roots must not inherit the app-server's launch recipe.
            config = routing_config(self.config, self.enabled())
            overrides = copy.deepcopy(params.get("config") or {})
            provider = config["model_provider"]
            headers = (
                overrides.setdefault("model_providers", {})
                .setdefault(provider, {})
                .setdefault("http_headers", {})
            )
            for name in list(headers):
                if name.casefold() == SMART_ROUTER_RECIPE_HEADER.casefold():
                    del headers[name]
            deep_merge_dict(
                overrides,
                {
                    "model_providers": {
                        provider: {
                            "http_headers": {
                                SMART_ROUTER_RECIPE_HEADER: config["responses_api_metadata"][
                                    "smart_router_recipe_name"
                                ]
                            }
                        }
                    },
                    "responses_api_metadata": config["responses_api_metadata"],
                },
            )
            params["config"] = overrides
        if (
            method in {"thread/start", "thread/resume", "thread/settings/update", "turn/start"}
            and "id" in message
        ):
            self.client_requests[message["id"]] = (method, copy.deepcopy(params))
            self.client_idle.clear()
        return message

    def remember_settings(self, source: dict, settings: ConversationSettings | None = None) -> None:
        settings = settings or self.settings
        if "input" in source:
            settings.turn.update(
                {
                    key: copy.deepcopy(source[key])
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
                    if key in source
                }
            )
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
                settings.resume[key] = copy.deepcopy(source[key])
        policy = source.get("sandboxPolicy") or source.get("sandbox")
        if policy is not None:
            settings.resume.pop("permissions", None)
            settings.turn.pop("permissions", None)
        if isinstance(policy, str):
            settings.resume["sandbox"] = policy
        elif isinstance(policy, dict):
            modes = {
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "dangerFullAccess": "danger-full-access",
            }
            mode = modes.get(policy.get("type"))
            if mode is None:
                if not source.get("activePermissionProfile"):
                    raise RuntimeError("Cannot preserve the current Codex sandbox profile")
            else:
                settings.resume["sandbox"] = mode
                settings.turn["sandboxPolicy"] = copy.deepcopy(policy)
            if mode == "workspace-write":
                settings.permissions["sandbox_workspace_write"] = {
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
            settings.resume["permissions"] = profile["id"]
            settings.turn["permissions"] = profile["id"]
        if "permissions" in settings.resume:
            settings.resume.pop("sandbox", None)
            settings.turn.pop("sandboxPolicy", None)

    def observe(self, message: dict) -> bool:
        identity = message.get("id")
        result = message.get("result") or {}
        params = message.get("params") or {}
        thread = result.get("thread") or params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            tid = thread["id"]
            self.remember_thread(thread)
        if "method" not in message and identity in self.client_requests:
            requested_method, requested = self.client_requests.pop(identity)
            if not self.client_requests:
                self.client_idle.set()
            if "error" not in message:
                if (
                    requested_method in {"thread/start", "thread/resume"}
                    and isinstance(thread, dict)
                    and not thread.get("ephemeral")
                    and not requested.get("ephemeral")
                ):
                    if self.root:
                        self.conversations[self.root] = self.settings
                    tid = thread["id"]
                    self.root = tid
                    self.settings = self.conversations.setdefault(tid, ConversationSettings())
                    if requested_method == "thread/resume":
                        # Resuming a loaded thread can ignore config overrides on reconnect.
                        self.reload_requested = True
                target = thread.get("id") if isinstance(thread, dict) else requested.get("threadId")
                settings = self.conversations.get(target)
                if target == self.root:
                    settings = self.settings
                if settings is not None:
                    self.remember_settings(requested, settings)
                    self.remember_settings(result, settings)
        method = message.get("method")
        tid = params.get("threadId")
        if method == "thread/settings/updated":
            settings = self.settings if tid == self.root else self.conversations.get(tid)
            if settings is not None:
                self.remember_settings(params.get("threadSettings") or {}, settings)
        if method == "turn/started" and isinstance(tid, str):
            self.active[tid] = params["turn"]["id"]
            if tid == self.root:
                self.last_turn = params["turn"]["id"]
        elif method == "turn/completed" and isinstance(tid, str):
            if self.active.get(tid) == params["turn"]["id"]:
                self.active.pop(tid)
        if (
            isinstance(identity, str)
            and identity.startswith(INTERNAL_REQUEST_PREFIX)
            and "method" not in message
        ):
            future = self.pending.get(identity)
            if future is not None and not future.done():
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

    async def rpc(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_SECONDS) -> dict:
        identity = f"{INTERNAL_REQUEST_PREFIX}{uuid.uuid4().hex}"
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

    def root_for(self, thread: str) -> str | None:
        seen = set()
        while thread and thread not in seen:
            if thread == self.root or thread in self.conversations:
                return thread
            seen.add(thread)
            thread = self.parents.get(thread) or ""
        return None

    def belongs_to_root(self, thread: str) -> bool:
        return self.root_for(thread) is not None

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
        self.cancel_continuation = False
        started = time.monotonic()
        self.log(f"[RELOAD] interrupting root={self.root} controls={self.controls}")
        async with asyncio.timeout(RELOAD_TIMEOUT_SECONDS):
            # Accepted requests can still change model or permissions before interruption.
            await self.client_idle.wait()
            if self.root is None:
                raise RuntimeError("No saved Codex conversation to reload")
            self.conversations[self.root] = self.settings
            roots = {self.root}
            interrupted_children: dict[str, set[str]] = {}
            interrupted_roots = set()
            while True:
                await self.unload(roots, interrupted_children, interrupted_roots)
                enabled = self.enabled()
                config = routing_config(self.config, enabled)
                for root in sorted(roots):
                    settings = self.conversations[root]
                    resumed_config = deep_merge_dict(copy.deepcopy(config), settings.permissions)
                    await self.rpc(
                        "thread/resume",
                        {
                            **settings.resume,
                            "threadId": root,
                            "config": resumed_config,
                            "excludeTurns": True,
                        },
                    )
                if enabled == self.enabled():
                    break
            self.applied = enabled
            self.reload_requested = False
            if not self.cancel_continuation:
                for root in sorted(roots):
                    if self.cancel_continuation:
                        break
                    if (
                        root == self.root
                        or root in interrupted_roots
                        or root in interrupted_children
                    ):
                        await self.continue_task(
                            root, enabled, interrupted_children.get(root, set())
                        )
            recipe = config["responses_api_metadata"]["smart_router_recipe_name"]
            self.log(
                f"[RELOAD] resumed root={self.root} recipe={recipe} elapsed={time.monotonic() - started:.3f}s"
            )
        self.reloading = False
        self.ready.set()

    async def unload(
        self,
        roots: set[str],
        interrupted_children: dict[str, set[str]],
        interrupted_roots: set[str],
    ) -> None:
        while True:
            tree = await self.loaded_tree()
            if not tree:
                return
            roots.update(root for tid in tree if (root := self.root_for(tid)) is not None)
            active = [(tid, turn) for tid, turn in self.active.items() if tid in tree]
            active.sort(key=lambda pair: pair[0] not in roots)
            for tid, turn in active:
                root = self.root_for(tid)
                if root is None:
                    continue
                if tid != root:
                    interrupted_children.setdefault(root, set()).add(self.task_paths.get(tid, tid))
                else:
                    interrupted_roots.add(root)
                try:
                    await self.rpc("turn/interrupt", {"threadId": tid, "turnId": turn})
                except RuntimeError:
                    if self.active.get(tid) == turn:
                        raise
            if not any(tid in self.active for tid in tree):
                for tid in sorted(tree, key=lambda tid: tid in roots):
                    await self.rpc("thread/unsubscribe", {"threadId": tid})
            await asyncio.sleep(0.05)

    async def continue_task(self, root: str, enabled: bool, interrupted_children: set[str]) -> None:
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
        if root in self.buffered_turns:
            self.continuations[root] = prompt
            return
        result = await self.rpc(
            "turn/start",
            {
                **self.conversations[root].turn,
                "threadId": root,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn_id = result.get("turn", {}).get("id")
        if self.cancel_continuation and turn_id and self.active.get(root) == turn_id:
            await self.rpc("turn/interrupt", {"threadId": root, "turnId": turn_id})

    async def watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                if self.root and (self.reload_requested or self.enabled() != self.applied):
                    await self.reload()
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            await self.report_failure(exc)
            raise

    async def report_failure(self, exc: Exception) -> None:
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
                            "message": f"Smart Router reload failed: {exc or 'reload timed out'}. Saved conversation: {self.root}. Exit and recover with: ug codex -- resume {self.root}. The new launch uses its own routing settings.",
                            "codexErrorInfo": None,
                            "additionalDetails": None,
                        },
                    },
                }
            )
        )
