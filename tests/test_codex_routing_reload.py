from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import socket
import subprocess
import sys
import time
import tomllib
from contextlib import suppress
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import codex_interposer, codex_reload, v2
from ucode.smart_routing.codex_reload import RoutingReload, routing_config

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
        self.on_resume = None
        self.on_list = None
        self.on_turn_start = None

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
        if "id" not in message or "method" not in message:
            return
        method, params = message["method"], message["params"]
        result = {}
        if method == "thread/loaded/list":
            if self.on_list:
                await self.on_list()
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
            if self.on_resume:
                self.on_resume()
            if self.resume_error:
                self.controller.observe(
                    {"id": message["id"], "error": {"message": "resume unavailable"}}
                )
                return
            result = {"thread": {"id": params["threadId"], "parentThreadId": None}}
            self.loaded[params["threadId"]] = result["thread"]
        elif method == "turn/start":
            turn = {"id": "continuation-turn"}
            self.controller.observe(
                {"method": "turn/started", "params": {"threadId": params["threadId"], "turn": turn}}
            )
            if self.on_turn_start:
                await self.on_turn_start()
            result = {"turn": turn}
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
        assert set(engine.loaded) == {"root", "other"}
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
        prompt = engine.calls[-1]["params"]["input"][0]["text"]
        assert "inspect existing effects" in prompt
        assert "rerun any command that did not exit successfully" in prompt
        assert "even if the latest user prompt only changed Smart Router" in prompt

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
                codex_interposer._Session(None, lambda _: None),
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


def test_rejected_permission_update_cannot_change_resumed_permissions(tmp_path):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        controller.remember_settings({"sandbox": "workspace-write", "approvalPolicy": "on-request"})
        await controller.before_tui(
            {
                "id": 8,
                "method": "thread/settings/update",
                "params": {
                    "threadId": "root",
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                    "approvalPolicy": "never",
                },
            }
        )
        assert not controller.client_idle.is_set()
        controller.observe({"id": 8, "error": {"message": "not allowed"}})
        assert controller.client_idle.is_set()
        await controller.reload()
        resumed = next(call["params"] for call in engine.calls if call["method"] == "thread/resume")
        assert resumed["sandbox"] == "workspace-write"
        assert resumed["approvalPolicy"] == "on-request"

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["response", "notification"])
def test_accepted_settings_replace_prior_turn_overrides_on_recovery(tmp_path, source):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        await controller.before_tui(
            {
                "id": 80,
                "method": "turn/start",
                "params": {
                    "threadId": "root",
                    "input": [{"type": "text", "text": "Earlier task"}],
                    "model": "old-model",
                    "approvalPolicy": "never",
                    "collaborationMode": {"mode": "plan", "settings": {"model": "plan-model"}},
                },
            }
        )
        controller.observe({"id": 80, "result": {"turn": {"id": "root-turn"}}})
        assert controller.turn_settings["collaborationMode"]["settings"]["model"] == "plan-model"
        accepted = {"model": "new-model", "approvalPolicy": "on-request"}
        if source == "response":
            await controller.before_tui(
                {
                    "id": 81,
                    "method": "thread/settings/update",
                    "params": {"threadId": "root", **accepted},
                }
            )
            controller.observe({"id": 81, "result": {}})
        else:
            controller.observe(
                {
                    "method": "thread/settings/updated",
                    "params": {"threadId": "root", "threadSettings": accepted},
                }
            )
        await controller.reload()
        resumed = next(call["params"] for call in engine.calls if call["method"] == "thread/resume")
        continued = next(call["params"] for call in engine.calls if call["method"] == "turn/start")
        for params in (resumed, continued):
            assert params["model"] == "new-model"
            assert params["approvalPolicy"] == "on-request"
        assert continued["collaborationMode"] == {
            "mode": "plan",
            "settings": {"model": "new-model"},
        }

    asyncio.run(scenario())


def test_switching_roots_reloads_each_tree_with_its_own_settings(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        controller.remember_settings(
            {"sandbox": "workspace-write", "cwd": "/first", "model": "first"}
        )
        await controller.before_tui(
            {
                "id": 9,
                "method": "thread/start",
                "params": {
                    "sandbox": "read-only",
                    "cwd": "/second",
                    "model": "second",
                },
            }
        )
        controller.observe({"id": 9, "result": {"thread": {"id": "second-root"}}})
        engine.add("second-root", None, active=False)
        engine.add("unrelated", None)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await controller.reload()
        resumed = {
            call["params"]["threadId"]: call["params"]
            for call in engine.calls
            if call["method"] == "thread/resume"
        }
        assert set(resumed) == {"root", "second-root"}
        assert resumed["root"]["sandbox"] == "workspace-write"
        assert resumed["root"]["model"] == "first"
        assert resumed["root"]["cwd"] == "/first"
        assert resumed["second-root"]["sandbox"] == "read-only"
        assert resumed["second-root"]["model"] == "second"
        assert resumed["second-root"]["cwd"] == "/second"
        assert "unrelated" in engine.loaded

    asyncio.run(scenario())


def test_new_root_while_off_gets_current_recipe_without_changing_other_overrides(tmp_path):
    async def scenario():
        controller, _, _, controls = make_controller(tmp_path)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        message = await controller.before_tui(
            {
                "id": 10,
                "method": "thread/start",
                "params": {
                    "config": {
                        "model_providers": {
                            "Databricks": {
                                "http_headers": {
                                    "Custom": "preserved",
                                    "databricks-smart-router-recipe": "stale",
                                }
                            }
                        },
                        "responses_api_metadata": {"custom": "preserved"},
                    },
                },
            }
        )
        config = message["params"]["config"]
        assert config["model_providers"]["Databricks"]["http_headers"] == {
            "Custom": "preserved",
            "Databricks-Smart-Router-Recipe": "DISABLED",
        }
        assert config["responses_api_metadata"] == {
            "custom": "preserved",
            "smart_router_recipe_name": "DISABLED",
        }

    asyncio.run(scenario())


def test_toggle_during_resume_is_applied_before_continuation(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        engine.on_resume = lambda: controls.write_text("{}")
        await controller.reload()
        recipes = [
            call["params"]["config"]["responses_api_metadata"]["smart_router_recipe_name"]
            for call in engine.calls
            if call["method"] == "thread/resume"
        ]
        assert recipes == ["DISABLED", "stable"]
        transitions = [
            (call["method"], call["params"]["threadId"])
            for call in engine.calls
            if call["method"] in {"thread/unsubscribe", "thread/resume"}
        ]
        first_resume = transitions.index(("thread/resume", "root"))
        assert transitions[first_resume:] == [
            ("thread/resume", "root"),
            ("thread/unsubscribe", "root"),
            ("thread/resume", "root"),
        ]
        continuations = [call for call in engine.calls if call["method"] == "turn/start"]
        assert len(continuations) == 1
        assert "Smart Router is now on" in continuations[0]["params"]["input"][0]["text"]
        assert controller.applied

    asyncio.run(scenario())


def test_reload_timeout_blocks_input_and_late_internal_reply_stays_private(tmp_path, monkeypatch):
    async def scenario():
        controller, engine, tui, controls = make_controller(tmp_path)
        monkeypatch.setattr(codex_reload, "RELOAD_TIMEOUT_SECONDS", 0.01)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        engine.on_list = asyncio.Event().wait
        with pytest.raises(TimeoutError):
            await controller.watch()
        assert not controller.ready.is_set()
        assert not controller.pending
        assert not any(call["method"] == "turn/start" for call in engine.calls)
        message = tui.calls[-1]["params"]["error"]["message"]
        assert "ug codex -- resume root" in message
        assert "reload timed out" in message
        identity = engine.calls[-1]["id"]
        assert controller.observe({"id": identity, "result": {"data": []}})

    asyncio.run(scenario())


def test_escape_during_reload_prevents_automatic_continuation(tmp_path):
    async def scenario():
        controller, engine, tui, _ = make_controller(tmp_path)

        async def cancel():
            assert (
                await controller.before_tui(
                    {
                        "id": 12,
                        "method": "turn/interrupt",
                        "params": {"threadId": "root", "turnId": "root-turn"},
                    }
                )
                is None
            )

        engine.on_list = cancel
        await controller.reload()
        assert any(call["method"] == "thread/resume" for call in engine.calls)
        assert not any(call["method"] == "turn/start" for call in engine.calls)
        assert {"id": 12, "result": {}} in tui.calls
        assert controller.ready.is_set()

    asyncio.run(scenario())


def test_escape_while_continuation_starts_interrupts_the_new_turn(tmp_path):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)

        async def escape():
            await controller.before_tui(
                {
                    "id": 13,
                    "method": "turn/interrupt",
                    "params": {"threadId": "root", "turnId": "root-turn"},
                }
            )

        engine.on_turn_start = escape
        await controller.reload()
        assert engine.calls[-1]["method"] == "turn/interrupt"
        assert engine.calls[-1]["params"]["turnId"] == "continuation-turn"
        assert "root" not in controller.active

    asyncio.run(scenario())


def test_recipe_headers_are_case_insensitive_and_launch_config_is_immutable():
    config = copy.deepcopy(CONFIG)
    headers = config["model_providers"]["Databricks"]["http_headers"]
    headers["databricks-smart-router-recipe"] = headers.pop("Databricks-Smart-Router-Recipe")
    original = copy.deepcopy(config)
    resumed = routing_config(config, enabled=True)
    assert resumed["responses_api_metadata"]["smart_router_recipe_name"] == "stable"
    assert resumed["model_providers"]["Databricks"]["http_headers"] == {
        "User-Agent": "preserve",
        "Databricks-Smart-Router-Recipe": "stable",
    }
    assert config == original


@pytest.mark.parametrize("arrival", ["before_reload", "during_reload"])
def test_buffered_user_turn_receives_recovery_context_without_competing_turn(tmp_path, arrival):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        pending = {
            "id": 20,
            "method": "turn/start",
            "params": {
                "threadId": "root",
                "input": [{"type": "text", "text": "Change the next step"}],
            },
        }

        forwarded = None
        if arrival == "before_reload":
            controller.buffer_input(pending)

        async def user_input():
            nonlocal forwarded
            engine.on_list = None
            if arrival == "during_reload":
                controller.buffer_input(pending)
            forwarded = asyncio.create_task(controller.before_tui(pending))
            await asyncio.sleep(0)
            assert not forwarded.done()

        engine.on_list = user_input
        await controller.reload()
        assert not any(call["method"] == "turn/start" for call in engine.calls)
        assert forwarded is not None
        message = await forwarded
        texts = [item["text"] for item in message["params"]["input"]]
        assert "Existing interrupted agents" in texts[0]
        assert texts[1] == "Change the next step"
        assert not controller.continuations

    asyncio.run(scenario())


def test_reload_waits_for_resume_preflight_and_accepted_response(tmp_path):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        engine.add("history", None, active=False)
        listing = asyncio.Event()
        release = asyncio.Event()

        async def hold_preflight():
            engine.on_list = None
            listing.set()
            await release.wait()

        engine.on_list = hold_preflight
        forwarding = asyncio.create_task(
            controller.before_tui(
                {"id": 90, "method": "thread/resume", "params": {"threadId": "history"}}
            )
        )
        await asyncio.wait_for(listing.wait(), 1)
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        reloading = asyncio.create_task(controller.reload())
        await asyncio.sleep(0)
        assert not controller.client_idle.is_set()
        assert [call["method"] for call in engine.calls] == ["thread/loaded/list"]
        release.set()
        await asyncio.wait_for(forwarding, 1)
        assert not reloading.done()
        assert not controller.client_idle.is_set()
        controller.observe(
            {"id": 90, "result": {"thread": engine.loaded["history"], "model": "history-model"}}
        )
        await asyncio.wait_for(reloading, 1)
        resumed = {
            call["params"]["threadId"]: call["params"]
            for call in engine.calls
            if call["method"] == "thread/resume"
        }
        assert resumed["history"]["model"] == "history-model"
        assert all(
            params["config"]["responses_api_metadata"]["smart_router_recipe_name"] == "DISABLED"
            for params in resumed.values()
        )

    asyncio.run(scenario())


def test_reconnected_root_is_reloaded_even_when_controls_have_not_changed(tmp_path):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        controller.root = None
        await controller.before_tui(
            {"id": 21, "method": "thread/resume", "params": {"threadId": "root"}}
        )
        controller.observe({"id": 21, "result": {"thread": {"id": "root"}}})
        resumed = asyncio.Event()
        engine.on_resume = resumed.set
        watcher = asyncio.create_task(controller.watch())
        try:
            await asyncio.wait_for(resumed.wait(), 1)
            await asyncio.wait_for(controller.ready.wait(), 1)
            assert not controller.reload_requested
            assert any(call["method"] == "turn/interrupt" for call in engine.calls)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("unsupported_sandbox", [False, True])
def test_resuming_idle_history_refreshes_attribution_without_starting_work(
    tmp_path, loaded, unsupported_sandbox
):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        controller.root = None
        controller.active.clear()
        engine.loaded.clear()
        if loaded:
            engine.add("history", None, active=False)
        forwarded = await controller.before_tui(
            {"id": 22, "method": "thread/resume", "params": {"threadId": "history"}}
        )
        assert (
            forwarded["params"]["config"]["responses_api_metadata"]["smart_router_recipe_name"]
            == "stable"
        )
        engine.loaded["history"] = {"id": "history", "parentThreadId": None}
        result = {"thread": engine.loaded["history"]}
        if unsupported_sandbox:
            result["sandbox"] = {"type": "externalSandbox"}
        controller.observe({"id": 22, "result": result})
        assert controller.reload_requested == loaded
        if loaded and unsupported_sandbox:
            with pytest.raises(RuntimeError, match="sandbox profile: externalSandbox"):
                await controller.reload()
            assert not any(
                call["method"] in {"thread/unsubscribe", "turn/interrupt"} for call in engine.calls
            )
            return
        if loaded:
            await controller.reload()
        assert not controller.reload_requested
        assert controller.ready.is_set()
        assert set(engine.loaded) == {"history"}
        assert not any(call["method"] in {"turn/interrupt", "turn/start"} for call in engine.calls)
        assert sum(call["method"] == "thread/unsubscribe" for call in engine.calls) == int(loaded)

    asyncio.run(scenario())


@pytest.mark.parametrize("parent_metadata", ["parentThreadId", "source", "previously_observed"])
@pytest.mark.parametrize("selected", ["child", "grandchild"])
def test_agent_navigation_preserves_tree_and_root_settings(tmp_path, parent_metadata, selected):
    async def scenario():
        controller, engine, _, controls = make_controller(tmp_path)
        controller.remember_settings({"model": "root-model", "cwd": "/root-workspace"})
        engine.add("grandchild", "child")
        original_settings = copy.deepcopy(controller.settings)
        thread = {"id": selected}
        parent = "root" if selected == "child" else "child"
        if parent_metadata == "parentThreadId":
            thread["parentThreadId"] = parent
        elif parent_metadata == "source":
            thread["source"] = {"subAgent": {"thread_spawn": {"parent_thread_id": parent}}}
        if parent_metadata != "previously_observed":
            controller.parents.pop(selected)
        for identity, viewed in enumerate([thread, {"id": "root", "parentThreadId": None}], 30):
            await controller.before_tui(
                {"id": identity, "method": "thread/resume", "params": {"threadId": viewed["id"]}}
            )
            controller.observe(
                {
                    "id": identity,
                    "result": {
                        "thread": viewed,
                        "model": "child-model" if viewed == thread else "root-model",
                        "cwd": "/child-workspace" if viewed == thread else "/root-workspace",
                    },
                }
            )
            assert controller.root == "root"
            assert controller.root_for(selected) == "root"
            assert controller.settings == original_settings
            assert not controller.reload_requested
        assert [call["method"] for call in engine.calls] == (
            [] if parent_metadata == "previously_observed" else ["thread/loaded/list"]
        )
        assert set(controller.active) == {"root", "child", "grandchild"}

        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await controller.reload()
        resumed = [call["params"] for call in engine.calls if call["method"] == "thread/resume"]
        assert len(resumed) == 1
        assert resumed[0]["threadId"] == "root"
        assert resumed[0]["model"] == "root-model"
        assert resumed[0]["cwd"] == "/root-workspace"
        assert (
            resumed[0]["config"]["responses_api_metadata"]["smart_router_recipe_name"] == "DISABLED"
        )
        interrupted = {
            call["params"]["threadId"]
            for call in engine.calls
            if call["method"] == "turn/interrupt"
        }
        assert interrupted == {"root", "child", "grandchild"}

    asyncio.run(scenario())


def test_returning_to_known_root_does_not_request_reconnect_reload(tmp_path):
    async def scenario():
        controller, _, _, _ = make_controller(tmp_path)
        for identity, method, tid in [
            (40, "thread/start", "second-root"),
            (41, "thread/resume", "root"),
        ]:
            await controller.before_tui(
                {"id": identity, "method": method, "params": {"threadId": tid}}
            )
            controller.observe({"id": identity, "result": {"thread": {"id": tid}}})
            assert not controller.reload_requested
        assert controller.root == "root"
        assert set(controller.conversations) == {"root", "second-root"}

    asyncio.run(scenario())


def test_viewing_known_child_does_not_override_its_configuration(tmp_path):
    async def scenario():
        controller, _, _, _ = make_controller(tmp_path)
        request = {"id": 42, "method": "thread/resume", "params": {"threadId": "child"}}
        forwarded = await controller.before_tui(copy.deepcopy(request))
        assert forwarded == request

    asyncio.run(scenario())


def test_shutdown_kills_unresponsive_server_with_bounded_wait():
    calls = []

    class Process:
        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            assert timeout == v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS
            calls.append("wait")
            if calls.count("wait") == 1:
                raise subprocess.TimeoutExpired("codex", timeout)

        def kill(self):
            calls.append("kill")

    v2._stop_codex_process(Process())
    assert calls == ["terminate", "wait", "kill", "wait"]


@pytest.mark.parametrize("wrapper_ignores_sigterm", [False, True])
def test_forced_shutdown_releases_native_descendants_listening_port(
    tmp_path, monkeypatch, wrapper_ignores_sigterm
):
    ready = tmp_path / "ready"
    child = """
import signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
Path(sys.argv[1] + '.tmp').write_text(str(listener.getsockname()[1]))
Path(sys.argv[1] + '.tmp').replace(sys.argv[1])
time.sleep(60)
"""
    wrapper = """
import signal, subprocess, sys
if sys.argv.pop(1) == 'ignore':
    signal.signal(signal.SIGTERM, lambda *_: None)
subprocess.Popen([sys.executable, '-c', *sys.argv[1:]]).wait()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            wrapper,
            "ignore" if wrapper_ignores_sigterm else "exit",
            child,
            str(ready),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline, "native descendant did not bind its port"
            time.sleep(0.01)
        port = int(ready.read_text())
        if os.name != "nt":
            monkeypatch.setattr(v2, "PROCESS_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
        v2._stop_codex_process(process, process_tree=True, port=port)
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
    finally:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5
            )
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.parametrize(
    "taskkill_result,diagnostic",
    [
        (0, None),
        (128, "Access denied"),
        (1, "Access denied"),
        (subprocess.TimeoutExpired("taskkill.exe", 5), "taskkill.exe.*timed out"),
        (OSError("taskkill.exe unavailable"), "taskkill.exe unavailable"),
    ],
)
@pytest.mark.parametrize("wait_times_out", [False, True])
def test_windows_shutdown_stops_native_process_tree(
    monkeypatch, taskkill_result, diagnostic, wait_times_out
):
    calls = []
    waits = []

    kills = []

    def wait(**kwargs):
        waits.append(kwargs)
        if wait_times_out and not kills:
            raise subprocess.TimeoutExpired("codex", kwargs["timeout"])

    process = SimpleNamespace(pid=42, poll=lambda: None, wait=wait, kill=lambda: kills.append(1))
    monkeypatch.setattr(v2, "os", SimpleNamespace(name="nt"))

    def taskkill(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(taskkill_result, Exception):
            raise taskkill_result
        return SimpleNamespace(returncode=taskkill_result, stderr="Access denied", stdout="")

    monkeypatch.setattr(v2.subprocess_cross_os, "run", taskkill)
    port_checks = []

    def port_closed(address, **kwargs):
        port_checks.append((address, kwargs))
        raise ConnectionRefusedError

    monkeypatch.setattr(v2.socket, "create_connection", port_closed)
    reports_failure = isinstance(taskkill_result, Exception) or (
        wait_times_out and taskkill_result != 0
    )
    if reports_failure:
        with pytest.raises(RuntimeError, match=f"Could not stop.*{diagnostic}"):
            v2._stop_codex_process(process, process_tree=True, port=12345)
    else:
        v2._stop_codex_process(process, process_tree=True, port=12345)
    assert port_checks == [((v2.LOOPBACK_HOST, 12345), {"timeout": 0.1})]
    # A failed taskkill still kills and reaps the wrapper before reporting.
    assert kills == ([1] if wait_times_out else [])
    assert waits == [{"timeout": v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS}] * (1 + len(kills))
    argv, options = calls[0]
    assert argv[0].endswith("taskkill.exe")
    assert argv[1:] == ["/PID", "42", "/T", "/F"]
    assert options["timeout"] == v2.PROCESS_SHUTDOWN_TIMEOUT_SECONDS
    assert options["capture_output"] and options["text"]


def test_shutdown_fails_if_the_server_port_is_still_listening(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            monkeypatch.setattr(v2, "PROCESS_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
            with pytest.raises(RuntimeError, match=f"port {port} is still in use"):
                v2._stop_codex_process(process, port=port)
            assert process.poll() is not None
        finally:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("unreadable", ["{", "[]", "null", "\\ud800"])
def test_transient_controls_failure_preserves_applied_state_and_recovers(tmp_path, unreadable):
    controller, _, _, controls = make_controller(tmp_path)
    logs = []
    controller.log = logs.append
    controller.applied = False
    controls.write_text(unreadable)
    assert controller.enabled() is False
    assert controller.enabled() is False
    assert len(logs) == 1
    controls.write_text("{}")
    assert controller.enabled() is True
    assert controller.controls_unreadable is False


@pytest.mark.parametrize(
    "message",
    [
        {"id": [], "method": "thread/resume", "params": {}},
        {"method": [], "params": {}},
        {"method": "turn/start", "params": []},
        {"method": "turn/start", "params": {"threadId": []}},
        {"id": [], "result": []},
        {"method": "turn/started", "params": {"threadId": "root", "turn": None}},
        {"method": "turn/completed", "params": {"threadId": "root", "turn": {}}},
    ],
)
def test_malformed_control_messages_do_not_change_session(tmp_path, message):
    controller, _, _, _ = make_controller(tmp_path)
    original = copy.deepcopy(message)
    assert asyncio.run(controller.before_tui(message)) == original
    controller.observe(message)
    assert controller.root == "root"
    assert controller.active["root"] == "root-turn"
    assert controller.client_idle.is_set()


@pytest.mark.parametrize(
    "failure",
    [None, "startup", "exit", "interrupt", "interrupt-timeout"],
)
def test_launcher_keeps_one_server_across_toggles_and_cleans_up_owned_processes(
    tmp_path, monkeypatch, failure
):
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(v2, "CODEX_APP_SERVER_LOG", tmp_path / "app-server.log")
    monkeypatch.setenv(v2.SESSION_PYTHON_ENV_VAR, "/test/python")
    monkeypatch.setattr(v2, "launch_token", lambda *_: "test-token")
    monkeypatch.setattr(v2, "_prepare_smart_router_session", lambda *_: controls)
    monkeypatch.setattr(v2, "_v2_hooks", lambda *_: CONFIG["hooks"])
    monkeypatch.setattr(v2, "_free_port", lambda: 41001)
    processes = []
    stopped = []
    ready_calls = []
    interposer_options = {}
    waits = 0

    def ready(*args, **kwargs):
        ready_calls.append(args)
        return failure != "startup"

    class Process:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.pid = 41000 + len(processes)
            self.env = kwargs.get("env")
            self.starts_session = kwargs.get("start_new_session", False)
            self.terminated = False
            self.killed = False
            self.signals = []
            processes.append(self)

        def poll(self):
            return 1 if failure == "exit" else None

        def wait(self, timeout=None):
            nonlocal waits
            if "--remote" not in self.argv:
                return 0
            if self.terminated:
                return 130
            if self.signals:
                if failure == "interrupt-timeout":
                    raise subprocess.TimeoutExpired(self.argv, timeout)
                return 130
            waits += 1
            if failure in ("interrupt", "interrupt-timeout"):
                raise KeyboardInterrupt
            if waits == 1:
                controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
            elif waits == 2:
                controls.write_text("{}")
            else:
                return 7
            raise subprocess.TimeoutExpired(self.argv, timeout)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def send_signal(self, value):
            self.signals.append(value)

    def start_interposer(*args, **kwargs):
        interposer_options.update(kwargs)
        return 41002, lambda: stopped.append(True)

    def taskkill(argv, **kwargs):
        assert argv[1:] == ["/PID", argv[2], "/T", "/F"]
        process = next(process for process in processes if str(process.pid) == argv[2])
        process.terminate()
        return SimpleNamespace(returncode=0)

    def killpg(pid, signum):
        process = next(process for process in processes if process.pid == pid)
        if signum == signal.SIGTERM:
            process.terminate()

    monkeypatch.setattr(v2, "os", SimpleNamespace(name=os.name, environ=os.environ, killpg=killpg))
    monkeypatch.setattr(v2, "_wait_for_app_server", ready)
    monkeypatch.setattr(v2.subprocess_cross_os, "popen", Process)
    monkeypatch.setattr(v2.subprocess_cross_os, "run", taskkill)
    monkeypatch.setattr(codex_interposer, "start_interposer_thread", start_interposer)

    def port_closed(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(v2.socket, "create_connection", port_closed)
    expected = SystemExit if failure in (None, "interrupt", "interrupt-timeout") else RuntimeError
    with pytest.raises(expected) as exc:
        v2.launch_codex(
            {"workspace": "https://example.databricks.com", "codex_models": ["gpt-original"]},
            [],
            binary="/test/codex",
            start_model="gpt-original",
            render_overlay=lambda *_a, **_kw: copy.deepcopy(CONFIG),
        )
    servers = [process for process in processes if "app-server" in process.argv]
    assert all(process.terminated for process in servers)
    if failure != "startup":
        assert stopped == [True]
    if failure == "exit":
        assert processes[1].terminated
    if failure in ("interrupt", "interrupt-timeout"):
        assert processes[1].signals == [v2.signal.SIGINT]
        assert exc.value.code == 130
        assert processes[1].terminated == (failure == "interrupt-timeout")
    assert len(servers) == 1
    if failure is None:
        assert exc.value.code == 7
        assert len(processes) == 2
        assert len(ready_calls) == 1
        assert servers[0].env["CODEX_HOME"] == str(tmp_path)
        assert servers[0].starts_session == (os.name != "nt")
        config = tomllib.loads("\n".join(servers[0].argv[3:-2:2]))
        assert config["thread_unload_delay_secs"] == 0
        assert interposer_options["session_controls"] == controls
        assert interposer_options["reload_config"]["responses_api_metadata"] == {
            "smart_router_recipe_name": "stable"
        }


def test_missing_launch_recipe_fails():
    config = copy.deepcopy(CONFIG)
    config["model_providers"]["Databricks"]["http_headers"] = {}
    with pytest.raises(RuntimeError, match="nonempty launch recipe"):
        routing_config(config, enabled=True)


def test_reload_preserves_the_model_selected_for_the_forwarded_first_prompt(tmp_path):
    async def scenario():
        controls = tmp_path / "env.json"
        controls.write_text("{}")
        calls = []
        resumed = asyncio.Event()

        async def engine(ws):
            async for raw in ws:
                message = json.loads(raw)
                calls.append(message)
                method = message["method"]
                result = {}
                if method in ("thread/start", "thread/resume"):
                    result = {"thread": {"id": "root"}}
                elif method == "thread/loaded/list":
                    result = {"data": [], "nextCursor": None}
                elif method == "turn/start":
                    result = {"turn": {"id": "turn"}}
                await ws.send(json.dumps({"id": message["id"], "result": result}))
                if method == "thread/resume":
                    resumed.set()

        async with serve(engine, "127.0.0.1", 0) as native:
            upstream = f"ws://127.0.0.1:{native.sockets[0].getsockname()[1]}"
            interposer = await codex_interposer._serve(
                "127.0.0.1",
                0,
                upstream,
                "gpt-selected",
                lambda _: None,
                reload_config=CONFIG,
                session_controls=controls,
            )
            uri = f"ws://127.0.0.1:{interposer.sockets[0].getsockname()[1]}"
            try:
                async with connect(uri) as tui:
                    await tui.send(json.dumps({"id": 1, "method": "thread/start", "params": {}}))
                    assert json.loads(await tui.recv())["id"] == 1
                    await tui.send(
                        json.dumps(
                            {
                                "id": 2,
                                "method": "turn/start",
                                "params": {
                                    "threadId": "root",
                                    "model": "gpt-original",
                                    "input": [{"type": "text", "text": "first prompt"}],
                                },
                            }
                        )
                    )
                    reply = json.loads(await tui.recv())
                    if str(reply["id"]).startswith("ucode-route-"):
                        reply = json.loads(await tui.recv())
                    assert reply["id"] == 2
                    controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
                    await resumed.wait()
                    resume = next(call for call in calls if call["method"] == "thread/resume")
                    assert resume["params"]["model"] == "gpt-selected"
            finally:
                interposer.close()
                await interposer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_interposer_reconnect_preserves_first_prompt_routing(monkeypatch):
    async def scenario():
        calls = []
        route_calls = []
        logs = []
        engine_connections = []
        passthrough = [
            b"binary",
            "{",
            "null",
            "[]",
            '{"id": [], "method": "thread/resume"}',
            '{"id": 0, "method": "thread/resume", "params": null}',
            '{"id": [], "result": null}',
        ]

        def decide(*args, **kwargs):
            route_calls.append(args)
            return codex_interposer.routing.RoutingDecision(
                model="gpt-selected", rationale="small"
            ), None

        monkeypatch.setattr(codex_interposer.codex_routing, "request_routing_decision", decide)

        async def engine(ws):
            engine_connections.append(ws)
            try:
                async for raw in ws:
                    if raw in passthrough:
                        await ws.send(raw)
                        continue
                    message = json.loads(raw)
                    calls.append(message)
                    method = message["method"]
                    result = {}
                    if method in ("thread/start", "thread/resume"):
                        result = {"thread": {"id": "root", "turns": []}}
                    if method == "turn/start":
                        result = {"turn": {"id": "turn"}}
                    await ws.send(json.dumps({"id": message["id"], "result": result}))
                    if method == "turn/start":
                        await ws.send(
                            json.dumps(
                                {
                                    "method": "turn/started",
                                    "params": {"threadId": "root", "turn": {"id": "turn"}},
                                }
                            )
                        )
            except ConnectionClosed:
                pass

        async with serve(engine, "127.0.0.1", 0) as native:
            upstream = f"ws://127.0.0.1:{native.sockets[0].getsockname()[1]}"
            interposer = await codex_interposer._serve(
                "127.0.0.1",
                0,
                upstream,
                None,
                logs.append,
                workspace="https://example.databricks.com",
                token_provider=lambda: "test-token",
            )
            uri = f"ws://127.0.0.1:{interposer.sockets[0].getsockname()[1]}"
            try:
                async with connect(uri) as tui:
                    for raw in passthrough:
                        await tui.send(raw)
                        assert await tui.recv() == raw
                    await tui.send(json.dumps({"id": 1, "method": "thread/start", "params": {}}))
                    assert json.loads(await tui.recv())["id"] == 1
                    await tui.send(
                        json.dumps(
                            {
                                "id": 2,
                                "method": "turn/start",
                                "params": {
                                    "threadId": "root",
                                    "model": "gpt-selected",
                                    "input": [{"type": "text", "text": "first prompt"}],
                                },
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 2
                    assert json.loads(await tui.recv())["method"] == "turn/started"
                    await engine_connections[-1].close(code=1012)
                    with pytest.raises(ConnectionClosed):
                        await tui.recv()
                async with connect(uri) as tui:
                    await tui.send(
                        json.dumps(
                            {
                                "id": 3,
                                "method": "thread/resume",
                                "params": {"threadId": "root", "sandbox": "workspace-write"},
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 3
                    await tui.send(
                        json.dumps(
                            {
                                "id": 4,
                                "method": "turn/start",
                                "params": {
                                    "threadId": "root",
                                    "model": "gpt-user-choice",
                                    "input": [{"type": "text", "text": "next prompt"}],
                                },
                            }
                        )
                    )
                    assert json.loads(await tui.recv())["id"] == 4
                    assert json.loads(await tui.recv())["method"] == "turn/started"
                assert len(route_calls) == 1
                assert calls[-1]["params"]["model"] == "gpt-user-choice"
                resume = next(call for call in calls if call["method"] == "thread/resume")
                assert resume["params"] == {"threadId": "root", "sandbox": "workspace-write"}
            finally:
                interposer.close()
                await interposer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_unknown_sandbox_keeps_session_running_until_reload(tmp_path):
    async def scenario():
        controller, engine, _, _ = make_controller(tmp_path)
        await controller.before_tui({"id": 1, "method": "thread/start", "params": {}})
        controller.observe(
            {
                "id": 1,
                "result": {
                    "thread": {"id": "root"},
                    "sandbox": {"type": "externalSandbox"},
                },
            }
        )
        assert controller.ready.is_set()
        assert controller.settings.unsupported_sandbox == "externalSandbox"
        with pytest.raises(RuntimeError, match="sandbox profile: externalSandbox"):
            await controller.reload()
        assert not any(call["method"] == "turn/interrupt" for call in engine.calls)
        controller.remember_settings({"sandbox": {"type": "workspaceWrite"}})
        assert controller.settings.unsupported_sandbox is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,reply",
    [
        ("thread/loaded/list", {"result": {}}),
        ("thread/loaded/list", {"result": {"data": None}}),
        ("thread/loaded/list", {"result": {"data": [None]}}),
        ("thread/loaded/list", {"result": {"data": [], "nextCursor": 1}}),
        ("thread/loaded/list", {"result": {"data": [], "nextCursor": "repeated"}}),
        ("thread/loaded/list", {"result": []}),
        ("thread/loaded/list", {"error": "unavailable"}),
        ("thread/read", {"result": {}}),
        ("thread/read", {"result": {"thread": {"id": "wrong-thread"}}}),
        ("thread/resume", {"result": {}}),
        ("thread/resume", {"result": {"thread": []}}),
        ("turn/start", {"result": {}}),
        ("turn/start", {"result": {"turn": []}}),
        ("turn/start", {"result": {"turn": {"id": None}}}),
    ],
)
def test_malformed_reload_reply_reports_recovery_before_disconnect(tmp_path, method, reply):
    async def scenario():
        controller, engine, tui, controls = make_controller(tmp_path)
        controller.parents.pop("child")
        send = engine.send

        async def malformed(raw):
            message = json.loads(raw)
            if message["method"] == method:
                controller.observe({"id": message["id"], **reply})
                return
            await send(raw)

        engine.send = malformed
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        with pytest.raises(RuntimeError, match=method):
            await asyncio.wait_for(controller.watch(), 1)
        error = tui.calls[-1]["params"]["error"]["message"]
        assert method in error
        assert "ug codex -- resume root" in error
        assert not controller.ready.is_set()
        assert not controller.pending
        assert not any(call["method"] == "turn/start" for call in engine.calls)

    asyncio.run(scenario())
