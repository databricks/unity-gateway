from __future__ import annotations

import asyncio
import copy
import json

import pytest

from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import codex_interposer
from ucode.smart_routing.codex_reload import RoutingReload

CONFIG = {
    "model_provider": "Databricks",
    "model_providers": {
        "Databricks": {
            "name": "Databricks",
            "base_url": "https://example.databricks.com/responses",
            "http_headers": {"Databricks-Smart-Router-Recipe": "stable", "User-Agent": "preserve"},
        }
    },
    "hooks": {"UserPromptSubmit": []},
}


class Peer:
    # The transport boundary controls races and RPC failures; the controller runs unchanged.
    def __init__(self):
        self.calls = []
        self.loaded = {}
        self.controller = None
        self.late_child = False
        self.resume_error = False

    def add(self, tid, parent, active=True):
        thread = {"id": tid, "parentThreadId": parent}
        self.loaded[tid] = thread
        self.controller.observe({"method": "thread/started", "params": {"thread": thread}})
        if active:
            self.controller.observe(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": tid,
                        "turn": {"id": tid + "-turn"},
                    },
                }
            )

    async def send(self, raw):
        message = json.loads(raw)
        self.calls.append(message)
        if "id" not in message:
            return
        method, params = message["method"], message["params"]
        result = {}
        if method == "thread/loaded/list":
            result = {"data": list(self.loaded), "nextCursor": None}
        elif method == "thread/read":
            result = {"thread": self.loaded[params["threadId"]]}
        elif method == "turn/interrupt":
            self.controller.observe(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": params["turnId"], "status": "interrupted"},
                    },
                }
            )
            if self.late_child and params["threadId"] == "root":
                self.add("late", "child")
                self.late_child = False
        elif method == "thread/unsubscribe":
            del self.loaded[params["threadId"]]
        elif method == "thread/resume":
            if self.resume_error:
                self.controller.observe(
                    {"id": message["id"], "error": {"message": "resume unavailable"}}
                )
                return
            result = {"thread": {"id": params["threadId"], "parentThreadId": None}}
        self.controller.observe({"id": message["id"], "result": result})


def make_controller(tmp_path):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    engine, tui = Peer(), Peer()
    controller = RoutingReload(engine, tui, CONFIG, controls, lambda _: None)
    engine.controller = controller
    controller.root = "root"
    engine.add("root", None)
    engine.add("child", "root")
    return controller, engine, tui, controls


def test_reload_catches_late_descendants_and_preserves_unrelated_session(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        engine.add("other", None)
        engine.late_child = True
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await controller.reload()
        interrupted = [
            m["params"]["threadId"] for m in engine.calls if m["method"] == "turn/interrupt"
        ]
        assert interrupted == ["root", "child", "late"]
        assert set(engine.loaded) == {"other"}
        resumed = next(m for m in engine.calls if m["method"] == "thread/resume")
        config = resumed["params"]["config"]
        assert config["model_providers"]["Databricks"]["http_headers"] == {
            "Databricks-Smart-Router-Recipe": "DISABLED",
            "User-Agent": "preserve",
        }
        assert config["hooks"] == CONFIG["hooks"]
        assert config["responses_api_metadata"]["smart_router_recipe_name"] == "DISABLED"
        assert controller.ready.is_set()
        assert controller.applied is False
        assert engine.calls[-1]["method"] == "turn/start"
        assert "inspect any completed effects" in engine.calls[-1]["params"]["input"][0]["text"]

    asyncio.run(scenario())


def test_off_on_restores_launch_recipe_without_mutating_launch_config(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        original = copy.deepcopy(CONFIG)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await controller.reload()
        engine.add("root", None)
        engine.add("child", "root")
        controls.write_text("{}")
        await controller.reload()
        configs = [m["params"]["config"] for m in engine.calls if m["method"] == "thread/resume"]
        assert [
            c["model_providers"]["Databricks"]["http_headers"]["Databricks-Smart-Router-Recipe"]
            for c in configs
        ] == ["DISABLED", "stable"]
        assert CONFIG == original
        assert controller.applied is True

    asyncio.run(scenario())


def test_reload_preserves_effective_permissions_and_working_directory(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        controller.remember_settings(
            {"sandbox": {"type": "readOnly"}, "activePermissionProfile": {"id": ":read-only"}}
        )
        policy = {
            "type": "workspaceWrite",
            "writableRoots": ["/extra"],
            "networkAccess": False,
            "excludeTmpdirEnvVar": True,
            "excludeSlashTmp": True,
        }
        controller.remember_settings(
            {"sandbox": policy, "approvalPolicy": "on-request", "cwd": "/project"}
        )
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await controller.reload()
        params = next(m["params"] for m in engine.calls if m["method"] == "thread/resume")
        assert params["sandbox"] == "workspace-write"
        assert "permissions" not in params
        assert params["approvalPolicy"] == "on-request"
        assert params["cwd"] == "/project"
        assert params["config"]["sandbox_workspace_write"] == {
            "writable_roots": ["/extra"],
            "network_access": False,
            "exclude_tmpdir_env_var": True,
            "exclude_slash_tmp": True,
        }
        assert engine.calls[-1]["params"]["sandboxPolicy"] == policy

    asyncio.run(scenario())


def test_failed_resume_blocks_input_and_reports_saved_conversation(tmp_path):
    async def scenario():
        controller, engine, tui, controls = make_controller(tmp_path)
        engine.resume_error = True
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        with pytest.raises(RuntimeError, match="resume unavailable"):
            await controller.watch()
        assert not controller.ready.is_set()
        assert not controller.pending
        assert not any(m["method"] == "turn/start" for m in engine.calls)
        assert "Saved conversation: root" in tui.calls[-1]["params"]["error"]["message"]

    asyncio.run(scenario())


def test_user_input_waits_for_reload_but_tool_responses_can_drain(tmp_path):
    async def scenario():
        controller, _, _, _ = make_controller(tmp_path)
        controller.ready.clear()
        pending = asyncio.create_task(
            controller.before_tui({"id": 4, "method": "turn/start", "params": {"threadId": "root"}})
        )
        await asyncio.sleep(0)
        assert not pending.done()
        await asyncio.wait_for(controller.before_tui({"id": 7, "result": {}}), 1)
        controller.ready.set()
        await asyncio.wait_for(pending, 1)

    asyncio.run(scenario())


def test_tui_background_thread_cannot_replace_root_or_permissions(tmp_path):
    async def scenario():
        controller, _, _, _ = make_controller(tmp_path)
        controller.remember_settings({"sandbox": "workspace-write", "approvalPolicy": "never"})
        await controller.before_tui(
            {
                "id": 5,
                "method": "thread/start",
                "params": {
                    "ephemeral": True,
                    "sandbox": "read-only",
                },
            }
        )
        controller.observe(
            {
                "id": 5,
                "result": {
                    "thread": {"id": "title-helper", "ephemeral": True},
                    "sandbox": {"type": "readOnly"},
                    "approvalPolicy": "on-request",
                },
            }
        )
        await controller.before_tui(
            {
                "id": 6,
                "method": "turn/start",
                "params": {
                    "threadId": "title-helper",
                    "sandboxPolicy": {"type": "readOnly"},
                },
            }
        )
        assert controller.root == "root"
        assert controller.resume_settings["sandbox"] == "workspace-write"
        assert controller.resume_settings["approvalPolicy"] == "never"
        assert not controller.belongs_to_root("title-helper")

    asyncio.run(scenario())


def test_interposer_drains_approval_reply_behind_held_request(tmp_path, monkeypatch):
    """The external transport stalls reload until a later TUI reply arrives."""

    async def scenario():
        controls = tmp_path / "env.json"
        controls.write_text("{}")
        reload_started = asyncio.Event()
        reply_received = asyncio.Event()
        turn_received = asyncio.Event()
        root_ready = asyncio.Event()
        held_rpc = None

        class Socket:
            def __init__(self, receive):
                self.incoming = asyncio.Queue()
                self.receive = receive

            async def send(self, raw):
                await self.receive(json.loads(raw))

            def __aiter__(self):
                return self

            async def __anext__(self):
                frame = await self.incoming.get()
                if frame is None:
                    raise StopAsyncIteration
                return json.dumps(frame)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

        async def receive_upstream(message):
            nonlocal held_rpc
            identity = message["id"]
            if identity == 7:
                reply_received.set()
                await upstream.incoming.put(
                    {"id": held_rpc, "result": {"data": [], "nextCursor": None}}
                )
                return
            method = message["method"]
            if method == "thread/loaded/list":
                held_rpc = identity
                reload_started.set()
                return
            result = {}
            if method in {"thread/start", "thread/resume"}:
                result = {"thread": {"id": "root"}}
            if identity == 3:
                turn_received.set()
            await upstream.incoming.put({"id": identity, "result": result})

        async def receive_tui(message):
            if message.get("id") == 1:
                root_ready.set()

        upstream = Socket(receive_upstream)
        tui = Socket(receive_tui)
        monkeypatch.setattr(codex_interposer, "connect", lambda *_a, **_kw: upstream)
        task = asyncio.create_task(
            codex_interposer._handle_tui(
                tui,
                "ws://external-transport",
                None,
                lambda _: None,
                reload_config=CONFIG,
                session_controls=controls,
            )
        )
        try:
            await tui.incoming.put({"id": 1, "method": "thread/start", "params": {}})
            await asyncio.wait_for(root_ready.wait(), 1)
            controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
            await asyncio.wait_for(reload_started.wait(), 1)
            await tui.incoming.put(
                {"id": 3, "method": "turn/start", "params": {"threadId": "root"}}
            )
            await tui.incoming.put({"id": 7, "result": {"decision": "accept"}})
            await asyncio.wait_for(reply_received.wait(), 1)
            await asyncio.wait_for(turn_received.wait(), 1)
            await tui.incoming.put(None)
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
