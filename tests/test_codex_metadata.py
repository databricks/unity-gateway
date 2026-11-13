"""Native metadata attribution and main-turn handoff behavior."""

import asyncio
import copy
import json
import threading
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import codex_interposer, codex_metadata, session_env
from ucode.smart_routing.codex_metadata import (
    ROUTING_STATE_KEY,
    RootTurn,
    RoutingMetadata,
    stamp_spawn,
)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("name", ["reviewer", "reviewer__sr_on", "reviewer__sr_on__sr_off"])
def test_spawn_stamp_preserves_routed_model_and_opaque_input(enabled, name):
    payload = {
        "tool_input": {"task_name": name, "message": {"encrypted": "opaque"}, "fork_turns": "all"}
    }
    output = {
        "systemMessage": "routing notice",
        "hookSpecificOutput": {"updatedInput": {"model": "gpt-6-sol"}},
    }
    before = copy.deepcopy((payload, output))
    stamped = stamp_spawn(payload, enabled, output)
    assert stamped["systemMessage"] == "routing notice"
    hook = stamped["hookSpecificOutput"]
    assert hook["permissionDecision"] == "allow"
    assert hook["updatedInput"] == {
        "task_name": f"reviewer__sr_{'on' if enabled else 'off'}",
        "message": {"encrypted": "opaque"},
        "fork_turns": "all",
        "model": "gpt-6-sol",
    }
    assert (payload, output) == before


@pytest.mark.parametrize("enabled", [False, True])
def test_real_hook_stamps_latest_shared_state_without_routing_when_disabled(
    tmp_path, monkeypatch, enabled
):
    controls = tmp_path / "controls.json"
    controls.write_text(json.dumps({} if enabled else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    token = Mock(return_value=None)
    route = Mock(side_effect=AssertionError("No token should mean no routing call"))
    monkeypatch.setattr(cli, "_router_hook_token", token)
    monkeypatch.setattr("ucode.smart_routing.codex_routing.route_pre_tool_use", route)
    result = CliRunner().invoke(
        cli.app,
        ["codex-router-hook", "route-subagent", "--host", "https://example.com"],
        input=json.dumps(
            {
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {"task_name": "nested", "message": "work"},
            }
        ),
    )
    assert result.exit_code == 0, result.output
    updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
    assert updated == {"task_name": f"nested__sr_{'on' if enabled else 'off'}", "message": "work"}
    assert token.call_count == int(enabled)
    route.assert_not_called()


class Wire:
    def __init__(self):
        self.messages = []
        self.on_send = None

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)
        if self.on_send:
            self.on_send(message)


def controller(tmp_path):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    upstream, tui = Wire(), Wire()
    metadata = RoutingMetadata(upstream, tui, controls, lambda _: None)
    return metadata, upstream, tui, controls


@pytest.mark.parametrize("entrypoint", ["initialize", "refresh", "turn"])
def test_controls_read_retries_denial_and_uses_latest_state(tmp_path, monkeypatch, entrypoint):
    async def run():
        metadata, upstream, tui, _ = controller(tmp_path)
        read = Mock(side_effect=[PermissionError("controls locked"), False])
        monkeypatch.setattr(codex_metadata, "session_routing_enabled", read)
        message = {"id": "input", "method": "turn/start", "params": {"threadId": "main"}}

        if entrypoint == "turn":
            forwarded = await asyncio.wait_for(metadata.before_tui(message), 2)
            assert forwarded["params"]["responsesapiClientMetadataPatch"] == {
                ROUTING_STATE_KEY: "off"
            }
        else:
            await asyncio.wait_for(getattr(metadata, entrypoint)(), 2)
            assert metadata.applied is False
        assert read.call_count == 2
        assert upstream.messages == tui.messages == []

    asyncio.run(run())


class StreamingWire(Wire):
    def __init__(self):
        super().__init__()
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.read_finished = asyncio.Event()
        self.closed = False
        self.tui_messages_at_close = []

    async def send(self, raw):
        await super().send(raw)
        self.outgoing.put_nowait(json.loads(raw))

    async def __aiter__(self):
        try:
            while (message := await self.incoming.get()) is not None:
                yield json.dumps(message)
        finally:
            self.read_finished.set()


@pytest.fixture
def running_interposer(tmp_path, monkeypatch):
    @asynccontextmanager
    async def run():
        controls = tmp_path / "controls.json"
        controls.write_text("{}")
        upstream, tui = StreamingWire(), StreamingWire()

        @asynccontextmanager
        async def connect(*args, **kwargs):
            try:
                yield upstream
            finally:
                upstream.tui_messages_at_close = list(tui.messages)
                upstream.closed = True

        monkeypatch.setattr(codex_interposer, "connect", connect)
        task = asyncio.create_task(
            codex_interposer._handle_tui(
                tui, "ws://localhost", None, lambda _: None, session_controls=controls
            )
        )
        try:
            for thread in ({"id": "main"}, {"id": "child", "parentThreadId": "main"}):
                message = {"result": {"thread": thread}}
                upstream.incoming.put_nowait(message)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == message
            yield task, upstream, tui, controls
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return run


def test_controls_recovery_gates_main_input_and_preserves_forwarding(
    running_interposer, monkeypatch
):
    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            retry_started = asyncio.Event()
            release_read = threading.Event()
            loop = asyncio.get_running_loop()
            original_read = codex_metadata.session_routing_enabled
            original_before_tui = RoutingMetadata.before_tui
            steering_started, steering_released = asyncio.Event(), asyncio.Event()
            attempts = 0

            async def before_tui(metadata, message):
                if message.get("id") != "steer":
                    return await original_before_tui(metadata, message)
                steering_started.set()
                forwarded = await original_before_tui(metadata, message)
                steering_released.set()
                return forwarded

            def read(path):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("controls locked")
                if attempts == 2:
                    loop.call_soon_threadsafe(retry_started.set)
                    if not release_read.wait(5):
                        raise TimeoutError("test did not release the controls read")
                return original_read(path)

            monkeypatch.setattr(codex_metadata, "session_routing_enabled", read)
            monkeypatch.setattr(RoutingMetadata, "before_tui", before_tui)
            try:
                await asyncio.wait_for(retry_started.wait(), 2)
                user_input = {
                    "id": "input",
                    "method": "turn/start",
                    "params": {"threadId": "main", "input": [{"type": "text", "text": "work"}]},
                }
                steering = {
                    "id": "steer",
                    "method": "turn/steer",
                    "params": {"threadId": "main", "input": [{"type": "text", "text": "wait"}]},
                }
                approval = {"id": "approval", "result": {"decision": "accept"}}
                child_interrupt = {
                    "id": "stop-child",
                    "method": "turn/interrupt",
                    "params": {"threadId": "child", "turnId": "child-turn"},
                }
                for message in (steering, user_input, approval, child_interrupt):
                    tui.incoming.put_nowait(message)
                await asyncio.wait_for(steering_started.wait(), 2)
                assert not steering_released.is_set()
                for expected in (approval, child_interrupt):
                    assert await asyncio.wait_for(upstream.outgoing.get(), 2) == expected
                assert upstream.messages == [approval, child_interrupt]
                child_update = {"method": "turn/started", "params": {"threadId": "child"}}
                upstream.incoming.put_nowait(child_update)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == child_update

                controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
                release_read.set()
                assert await asyncio.wait_for(upstream.outgoing.get(), 2) == steering
                forwarded = await asyncio.wait_for(upstream.outgoing.get(), 2)
                assert forwarded == {
                    **user_input,
                    "params": {
                        **user_input["params"],
                        "responsesapiClientMetadataPatch": {ROUTING_STATE_KEY: "off"},
                    },
                }
                assert all(message.get("method") != "error" for message in tui.messages)
                tui.incoming.put_nowait(None)
                await asyncio.wait_for(task, 2)
                assert upstream.closed
                assert upstream.read_finished.is_set() and tui.read_finished.is_set()
            finally:
                release_read.set()

    asyncio.run(run())


@pytest.mark.parametrize("entrypoint", ["poll", "turn"])
def test_persistent_controls_denial_reports_recovery_before_teardown(
    running_interposer, monkeypatch, entrypoint
):
    async def wait_for_poll(self):
        await asyncio.Event().wait()

    if entrypoint == "turn":
        monkeypatch.setattr(RoutingMetadata, "watch", wait_for_poll)

    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            read = Mock(side_effect=PermissionError("controls locked"))
            monkeypatch.setattr(codex_metadata, "session_routing_enabled", read)
            if entrypoint == "turn":
                tui.incoming.put_nowait(
                    {"id": "input", "method": "turn/start", "params": {"threadId": "main"}}
                )

            error = await asyncio.wait_for(tui.outgoing.get(), 2)
            assert error["method"] == "error"
            assert error["params"]["willRetry"] is False
            assert str(controls) in error["params"]["message"]
            assert "controls locked" in error["params"]["message"]
            assert "restart Codex through ug" in error["params"]["message"]
            with pytest.raises(PermissionError, match="controls locked"):
                await asyncio.wait_for(task, 2)

            assert read.call_count == codex_metadata.CONTROLS_READ_ATTEMPTS
            assert upstream.messages == []
            assert [message for message in tui.messages if message.get("method") == "error"] == [
                error
            ]
            assert error in upstream.tui_messages_at_close
            assert upstream.closed
            assert upstream.read_finished.is_set() and tui.read_finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("existing_patch", [None, {"other": "kept"}])
def test_turn_metadata_preserves_other_fields_and_does_not_modify_child_input(
    tmp_path, existing_patch
):
    async def run():
        metadata, _, _, controls = controller(tmp_path)
        metadata.roots["main"] = RootTurn()
        metadata.children.add("child")
        for enabled in (True, False, True):
            controls.write_text(
                json.dumps({} if enabled else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
            )
            message = {
                "id": "start",
                "method": "turn/start",
                "params": {
                    "threadId": "main",
                    "input": [{"type": "text", "text": "work"}],
                    "model": "chosen",
                    "responsesapiClientMetadata": {"caller": "kept"},
                    "responsesapiClientMetadataPatch": existing_patch,
                },
            }
            forwarded = await metadata.before_tui(copy.deepcopy(message))
            expected = copy.deepcopy(message)
            expected["params"]["responsesapiClientMetadataPatch"] = {
                **(existing_patch or {}),
                ROUTING_STATE_KEY: "on" if enabled else "off",
            }
            assert forwarded == expected
            child = {
                "id": "child-start",
                "method": "turn/start",
                "params": {"threadId": "child", "input": []},
            }
            assert await metadata.before_tui(copy.deepcopy(child)) == child

    asyncio.run(run())


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_toggle_interrupts_only_main_and_preserves_conversation_tree(
    tmp_path, queued, cancel, enabled
):
    async def run():
        metadata, upstream, tui, controls = controller(tmp_path)
        controls.write_text(
            json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0") if enabled else {})
        )
        await metadata.initialize()
        metadata.roots["main"] = RootTurn(active="main-turn")
        metadata.children.add("child")
        request = {
            "id": "toggle",
            "method": "turn/start",
            "params": {
                "threadId": "main",
                "input": [
                    {"type": "text", "text": f"$smart-router {'on' if enabled else 'off'}"},
                    {"type": "skill", "name": "smart-router"},
                ],
            },
        }
        forwarded = await metadata.before_tui(copy.deepcopy(request))
        assert forwarded["params"]["input"] == request["params"]["input"]
        metadata.observe({"id": "toggle", "result": {"turn": {"id": "main-turn"}}})
        child_turn = {
            "method": "turn/started",
            "params": {"threadId": "child", "turn": {"id": "child-turn"}},
        }
        assert metadata.observe(child_turn) is False
        controls.write_text(
            json.dumps({} if enabled else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"))
        )

        def reply(message):
            if message["method"] == "turn/interrupt":
                metadata.observe(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "main", "turn": {"id": "main-turn"}},
                    }
                )
                result = {}
            else:
                pytest.fail(f"Unexpected automatic request: {message['method']}")
            metadata.observe({"id": message["id"], "result": result})

        upstream.on_send = reply
        metadata.client_idle.clear()
        task = asyncio.create_task(metadata.refresh())
        await asyncio.sleep(0)
        queued_input = None
        if queued:
            queued_input = asyncio.create_task(
                metadata.before_tui(
                    {
                        "id": "user-start",
                        "method": "turn/start",
                        "params": {
                            "threadId": "main",
                            "input": [{"type": "text", "text": "new work"}],
                        },
                    }
                )
            )
            await asyncio.sleep(0)
            assert not queued_input.done()
        if cancel:
            assert (
                await metadata.before_tui(
                    {
                        "id": "escape",
                        "method": "turn/interrupt",
                        "params": {"threadId": "main", "turnId": "main-turn"},
                    }
                )
                is None
            )
            assert tui.messages == [{"id": "escape", "result": {}}]
        metadata.client_idle.set()
        await task
        assert metadata.applied is enabled
        assert metadata.ready.is_set()
        assert "main" in metadata.roots and "child" in metadata.children
        assert all(message["params"]["threadId"] == "main" for message in upstream.messages)
        assert all(
            message["method"] not in {"thread/unsubscribe", "thread/resume"}
            for message in upstream.messages
        )
        assert [message["method"] for message in upstream.messages] == ["turn/interrupt"]
        if queued_input is not None:
            forwarded = await queued_input
            assert forwarded["params"]["input"] == [{"type": "text", "text": "new work"}]
            assert forwarded["params"]["responsesapiClientMetadataPatch"] == {
                ROUTING_STATE_KEY: "on" if enabled else "off"
            }

    asyncio.run(run())


def test_idle_toggle_does_not_start_inference_or_add_a_prompt(tmp_path):
    async def run():
        metadata, upstream, _, controls = controller(tmp_path)
        metadata.roots["main"] = RootTurn()
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        await metadata.refresh()
        assert upstream.messages == []
        assert metadata.applied is False
        assert metadata.ready.is_set()

    asyncio.run(run())


def test_child_navigation_does_not_become_main_and_internal_replies_stay_private(tmp_path):
    metadata, _, _, _ = controller(tmp_path)
    metadata.observe({"id": 1, "result": {"thread": {"id": "main", "source": "cli"}}})
    metadata.observe(
        {
            "id": 2,
            "result": {
                "thread": {
                    "id": "child",
                    "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "main"}}},
                }
            },
        }
    )
    assert list(metadata.roots) == ["main"]
    assert metadata.children == {"child"}
    assert metadata.observe({"id": "ug-routing-metadata-private", "result": {}})


def test_failed_update_keeps_new_input_blocked_but_allows_child_interrupts(tmp_path):
    async def run():
        metadata, upstream, _, _ = controller(tmp_path)
        metadata.roots["main"] = RootTurn(active="turn")
        metadata.children.add("child")

        def reject(message):
            metadata.observe({"id": message["id"], "error": {"message": "rejected"}})

        upstream.on_send = reject
        with pytest.raises(RuntimeError, match="rejected"):
            await metadata.refresh()
        assert not metadata.ready.is_set()
        child_interrupt = {
            "id": "stop-child",
            "method": "turn/interrupt",
            "params": {"threadId": "child", "turnId": "child-turn"},
        }
        assert await asyncio.wait_for(metadata.before_tui(child_interrupt), 1) == child_interrupt
        approval = {"id": "approve", "result": {"decision": "accept"}}
        assert await metadata.before_tui(approval) == approval
        pending = asyncio.create_task(
            metadata.before_tui(
                {"id": "input", "method": "turn/start", "params": {"threadId": "main"}}
            )
        )
        await asyncio.sleep(0)
        assert not pending.done()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_toggle_command_finishes_before_main_is_interrupted(tmp_path):
    async def run():
        metadata, upstream, _, _ = controller(tmp_path)
        metadata.roots["main"] = RootTurn(active="turn")
        item = {
            "type": "commandExecution",
            "id": "toggle",
            "command": '"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing',
        }
        metadata.observe({"method": "item/started", "params": {"threadId": "main", "item": item}})
        task = asyncio.create_task(metadata.refresh())
        await asyncio.sleep(0)
        assert upstream.messages == []

        def reply(message):
            metadata.observe({"id": message["id"], "result": {}})
            metadata.observe(
                {"method": "turn/completed", "params": {"threadId": "main", "turn": {"id": "turn"}}}
            )

        upstream.on_send = reply
        metadata.observe({"method": "item/completed", "params": {"threadId": "main", "item": item}})
        await task
        assert [message["method"] for message in upstream.messages] == ["turn/interrupt"]

    asyncio.run(run())
