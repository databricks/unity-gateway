"""Native metadata attribution at main-turn and subagent-spawn boundaries."""

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

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)


def controller(tmp_path):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    upstream, tui = Wire(), Wire()
    metadata = RoutingMetadata(tui, controls, lambda _: None)
    return metadata, upstream, tui, controls


@pytest.mark.parametrize("entrypoint", ["startup", "turn"])
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
            assert await asyncio.wait_for(metadata.enabled(), 2) is False
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
            attempts = 0

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
            try:
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
                main_interrupt = {
                    "id": "escape",
                    "method": "turn/interrupt",
                    "params": {"threadId": "main", "turnId": "main-turn"},
                }
                for message in (user_input, steering, approval, child_interrupt, main_interrupt):
                    tui.incoming.put_nowait(message)
                await asyncio.wait_for(retry_started.wait(), 2)
                for expected in (approval, child_interrupt, main_interrupt):
                    assert await asyncio.wait_for(upstream.outgoing.get(), 2) == expected
                assert upstream.messages == [approval, child_interrupt, main_interrupt]
                child_update = {"method": "turn/started", "params": {"threadId": "child"}}
                upstream.incoming.put_nowait(child_update)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == child_update

                controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
                release_read.set()
                forwarded = await asyncio.wait_for(upstream.outgoing.get(), 2)
                assert forwarded == {
                    **user_input,
                    "params": {
                        **user_input["params"],
                        "responsesapiClientMetadataPatch": {ROUTING_STATE_KEY: "off"},
                    },
                }
                assert await asyncio.wait_for(upstream.outgoing.get(), 2) == steering
                assert all(message.get("method") != "error" for message in tui.messages)
                tui.incoming.put_nowait(None)
                await asyncio.wait_for(task, 2)
                assert upstream.closed
                assert upstream.read_finished.is_set() and tui.read_finished.is_set()
            finally:
                release_read.set()

    asyncio.run(run())


def test_persistent_per_turn_controls_denial_reports_recovery_before_teardown(
    running_interposer, monkeypatch
):
    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            read = Mock(side_effect=PermissionError("controls locked"))
            monkeypatch.setattr(codex_metadata, "session_routing_enabled", read)
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


def test_toggle_finishes_normally_and_next_turn_uses_latest_state(running_interposer):
    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            start = {"id": "toggle", "method": "turn/start", "params": {"threadId": "main"}}
            tui.incoming.put_nowait(start)
            first = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert first["params"]["responsesapiClientMetadataPatch"] == {ROUTING_STATE_KEY: "on"}
            started = {
                "method": "turn/started",
                "params": {"threadId": "main", "turn": {"id": "toggle-turn"}},
            }
            upstream.incoming.put_nowait(started)
            assert await asyncio.wait_for(tui.outgoing.get(), 2) == started

            controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
            confirmation = {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "main", "delta": "Smart Router is off."},
            }
            completed = {
                "method": "turn/completed",
                "params": {"threadId": "main", "turn": {"id": "toggle-turn"}},
            }
            for message in (confirmation, completed):
                upstream.incoming.put_nowait(message)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == message
            assert upstream.messages == [first]

            next_turn = {"id": "next", "method": "turn/start", "params": {"threadId": "main"}}
            tui.incoming.put_nowait(next_turn)
            second = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert second["params"]["responsesapiClientMetadataPatch"] == {ROUTING_STATE_KEY: "off"}
            assert upstream.messages == [first, second]
            tui.incoming.put_nowait(None)
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_child_navigation_does_not_change_child_attribution(tmp_path):
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
    assert metadata.children == {"child"}
