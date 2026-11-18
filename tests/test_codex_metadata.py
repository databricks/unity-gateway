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
from ucode.smart_routing import codex_interposer, session_env
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


@pytest.mark.parametrize("failure", ["transient", "permission", "invalid", "missing"])
def test_hook_controls_failure_never_stamps_inherited_launch_state(tmp_path, monkeypatch, failure):
    controls = tmp_path / "controls.json"
    disabled = dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
    controls.write_text(json.dumps(disabled))
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    monkeypatch.setattr(session_env, "CONTROLS_READ_RETRY_SECONDS", 0)
    errors = {
        "transient": [PermissionError("controls locked"), disabled],
        "permission": PermissionError("controls locked"),
        "invalid": ValueError("invalid controls"),
        "missing": FileNotFoundError("controls missing"),
    }
    read = Mock(side_effect=errors[failure])
    monkeypatch.setattr(session_env, "_read", read)
    token = Mock(side_effect=AssertionError("Unknown or off controls must not route"))
    monkeypatch.setattr(cli, "_router_hook_token", token)
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
    assert result.stderr == ""
    if failure == "transient":
        updated = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
        assert updated == {"task_name": "nested__sr_off", "message": "work"}
    else:
        assert result.stdout == ""
    attempts = {"transient": 2, "permission": session_env.CONTROLS_READ_ATTEMPTS}
    assert read.call_count == attempts.get(failure, 1)
    token.assert_not_called()


class Wire:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)


def controller(tmp_path):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    tui = Wire()
    metadata = RoutingMetadata(controls, lambda _: None)
    return metadata, tui, controls


@pytest.mark.parametrize("entrypoint", ["read", "turn"])
def test_controls_read_retries_denial_and_uses_latest_state(tmp_path, monkeypatch, entrypoint):
    async def run():
        metadata, tui, _ = controller(tmp_path)
        read = Mock(
            side_effect=[
                PermissionError("controls locked"),
                dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"),
            ]
        )
        monkeypatch.setattr(session_env, "_read", read)
        message = {"id": "input", "method": "turn/start", "params": {"threadId": "main"}}

        if entrypoint == "turn":
            forwarded = await asyncio.wait_for(metadata.before_tui(message), 2)
            assert forwarded["params"]["responsesapiClientMetadataPatch"] == {
                ROUTING_STATE_KEY: "off"
            }
        else:
            assert await asyncio.wait_for(metadata.enabled(), 2) is False
        assert read.call_count == 2
        assert tui.messages == []

    asyncio.run(run())


class StreamingWire(Wire):
    def __init__(self):
        super().__init__()
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.read_finished = asyncio.Event()
        self.closed = False

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
    async def run(*, controls_text="{}", log=lambda _: None):
        controls = tmp_path / "controls.json"
        if controls_text is not None:
            controls.write_text(controls_text)
        upstream, tui = StreamingWire(), StreamingWire()

        @asynccontextmanager
        async def connect(*args, **kwargs):
            try:
                yield upstream
            finally:
                upstream.closed = True

        monkeypatch.setattr(codex_interposer, "connect", connect)
        task = asyncio.create_task(
            codex_interposer._handle_tui(
                tui, "ws://localhost", None, log, session_controls=controls
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
            original_read = session_env._read
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

            monkeypatch.setattr(session_env, "_read", read)
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


@pytest.mark.parametrize("controls_text", [None, "{invalid"])
def test_bad_controls_do_not_prevent_connection(running_interposer, controls_text):
    async def run():
        async with running_interposer(controls_text=controls_text) as (task, upstream, tui, _):
            initialize = {"id": "init", "method": "initialize", "params": {"clientInfo": {}}}
            tui.incoming.put_nowait(initialize)
            forwarded = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert forwarded["params"]["capabilities"] == {"experimentalApi": True}
            assert not task.done()
            assert not upstream.closed
            assert all(message.get("method") != "error" for message in tui.messages)
            tui.incoming.put_nowait(None)
            await asyncio.wait_for(task, 2)

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("controls locked"),
        ValueError("invalid controls"),
        FileNotFoundError("missing"),
    ],
    ids=["permission", "invalid", "missing"],
)
def test_controls_failure_is_silent_and_recovers_on_next_turn(
    running_interposer, monkeypatch, error
):
    async def run():
        logs = []
        async with running_interposer(log=logs.append) as (task, upstream, tui, controls):
            read = Mock(side_effect=error)
            message = {
                "id": "input",
                "method": "turn/start",
                "params": {
                    "threadId": "main",
                    "responsesapiClientMetadataPatch": {"other": "kept"},
                },
            }
            with monkeypatch.context() as denied:
                denied.setattr(session_env, "_read", read)
                tui.incoming.put_nowait(message)
                assert await asyncio.wait_for(upstream.outgoing.get(), 2) == message
            attempts = (
                session_env.CONTROLS_READ_ATTEMPTS if isinstance(error, PermissionError) else 1
            )
            assert read.call_count == attempts
            assert not task.done()
            assert not upstream.closed
            assert any(str(controls) in line and str(error) in line for line in logs)

            controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
            next_turn = {"id": "next", "method": "turn/start", "params": {"threadId": "main"}}
            tui.incoming.put_nowait(next_turn)
            forwarded = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert forwarded["params"]["responsesapiClientMetadataPatch"] == {
                ROUTING_STATE_KEY: "off"
            }
            assert upstream.messages == [message, forwarded]
            assert all(message.get("method") != "error" for message in tui.messages)
            tui.incoming.put_nowait(None)
            await asyncio.wait_for(task, 2)
            assert upstream.closed
            assert upstream.read_finished.is_set() and tui.read_finished.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("existing_patch", [None, {"other": "kept"}])
def test_turn_metadata_preserves_other_fields_and_does_not_modify_child_input(
    tmp_path, existing_patch
):
    async def run():
        metadata, _, controls = controller(tmp_path)
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
    metadata, _, _ = controller(tmp_path)
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


async def register_native_router(upstream, tui):
    thread_id = "1cbd36a0-177f-4df1-a5c2-5495a86a04a7"
    tui.incoming.put_nowait({"id": "native-start", "method": "thread/start", "params": {}})
    request = await asyncio.wait_for(upstream.outgoing.get(), 2)
    assert request["params"]["dynamicTools"][0]["name"] == "ug_smart_router"
    reply = {"id": "native-start", "result": {"thread": {"id": thread_id}}}
    upstream.incoming.put_nowait(reply)
    assert await asyncio.wait_for(tui.outgoing.get(), 2) == reply
    return thread_id


def native_tool_request(thread_id, action="off"):
    return {
        "id": "native-call",
        "method": "item/tool/call",
        "params": {
            "threadId": thread_id,
            "turnId": "turn",
            "callId": "call",
            "tool": "ug_smart_router",
            "arguments": {"action": action},
        },
    }


def test_native_tool_is_serviced_by_host_and_other_requests_forward(
    running_interposer, monkeypatch
):
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "0")

    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            thread_id = await register_native_router(upstream, tui)
            request = native_tool_request(thread_id)
            upstream.incoming.put_nowait(request)
            response = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert response["id"] == request["id"]
            assert response["result"]["success"] is True
            assert "Smart Router is off" in response["result"]["contentItems"][0]["text"]
            assert json.loads(controls.read_text()) == dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")
            foreign = native_tool_request(thread_id)
            foreign["params"]["tool"] = "another_tool"
            upstream.incoming.put_nowait(foreign)
            assert await asyncio.wait_for(tui.outgoing.get(), 2) == foreign
            assert request not in tui.messages
            assert not task.done()

    asyncio.run(run())


def test_native_tool_capture_does_not_block_interrupts_or_other_frames(
    running_interposer, monkeypatch
):
    started, release = threading.Event(), threading.Event()

    def execute(self, params):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release capture")
        return {"message": "capture finished"}

    monkeypatch.setattr(codex_interposer.CodexSmartRouter, "execute", execute)

    async def run():
        async with running_interposer() as (_, upstream, tui, _):
            thread_id = await register_native_router(upstream, tui)
            upstream.incoming.put_nowait(native_tool_request(thread_id))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                interrupt = {
                    "id": "interrupt",
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": "turn"},
                }
                tui.incoming.put_nowait(interrupt)
                assert await asyncio.wait_for(upstream.outgoing.get(), 2) == interrupt
                notice = {"method": "turn/completed", "params": {"threadId": thread_id}}
                upstream.incoming.put_nowait(notice)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == notice
            finally:
                release.set()
            reply = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert reply["result"]["success"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    "delivery,error", [("submitted", False), ("unconfirmed", False), ("unconfirmed", True)]
)
def test_native_delivery_notice_after_turn_without_model_or_shell(
    running_interposer, monkeypatch, delivery, error
):
    monkeypatch.setattr(
        codex_interposer.CodexSmartRouter,
        "execute",
        lambda *_: {"delivery": "pending", "feedback_id": "report"},
    )
    checked = threading.Event()

    def status(_self, _report_id):
        checked.set()
        if error:
            raise OSError("status unavailable")
        return {"delivery": delivery, "message": "Feedback " + delivery}

    monkeypatch.setattr(codex_interposer.CodexSmartRouter, "delivery", status)

    async def run():
        async with running_interposer() as (_, upstream, tui, _):
            thread_id = await register_native_router(upstream, tui)
            upstream.incoming.put_nowait(native_tool_request(thread_id))
            assert (await asyncio.wait_for(upstream.outgoing.get(), 2))["result"]["success"]
            assert await asyncio.to_thread(checked.wait, 3)
            await asyncio.sleep(0.05)
            assert tui.outgoing.empty(), "delivery must wait for the model's final reply"
            completed = {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {"id": "turn", "status": "completed"}},
            }
            upstream.incoming.put_nowait(completed)
            assert await asyncio.wait_for(tui.outgoing.get(), 2) == completed
            started = await asyncio.wait_for(tui.outgoing.get(), 3)
            ended = await asyncio.wait_for(tui.outgoing.get(), 2)
            assert started["method"] == "item/started"
            assert ended["method"] == "item/completed"
            assert isinstance(started["params"]["startedAtMs"], int)
            assert ended["params"]["completedAtMs"] >= started["params"]["startedAtMs"]
            assert ended["params"]["item"] == started["params"]["item"]
            text = ended["params"]["item"]["text"]
            assert ("could not be confirmed" if error else delivery) in text
            assert upstream.outgoing.empty(), "notices must not start a new model turn"

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["cancel", "interrupt", "completed", "disconnect", "error"])
def test_native_selector_replies_stay_local_and_cancel_cleanly(
    running_interposer, monkeypatch, ending
):
    calls = []

    def execute(self, params):
        calls.append(params["arguments"])
        return (
            {"needs_reason": True}
            if params["arguments"]["action"] == "off"
            else {"message": "Canceled"}
        )

    monkeypatch.setattr(codex_interposer.CodexSmartRouter, "execute", execute)

    async def run():
        async with running_interposer() as (task, upstream, tui, controls):
            thread_id = await register_native_router(upstream, tui)
            upstream.incoming.put_nowait(native_tool_request(thread_id))
            prompt = await asyncio.wait_for(tui.outgoing.get(), 2)
            assert prompt["method"] == "item/tool/requestUserInput"
            assert prompt["params"]["threadId"] == thread_id
            assert prompt["params"]["turnId"] == "turn"
            question = prompt["params"]["questions"][0]
            assert [option["label"] for option in question["options"]] == [
                "Provide a reason",
                "Cancel",
            ]
            approval = {"id": "unrelated-approval", "result": {"decision": "accept"}}
            tui.incoming.put_nowait(approval)
            assert await asyncio.wait_for(upstream.outgoing.get(), 2) == approval
            if ending == "disconnect":
                tui.incoming.put_nowait(None)
                await asyncio.wait_for(task, 2)
                assert calls == [{"action": "off"}]
                assert upstream.outgoing.empty()
                return
            if ending == "interrupt":
                message = {
                    "id": "escape",
                    "method": "turn/interrupt",
                    "params": {"threadId": thread_id, "turnId": "turn"},
                }
                tui.incoming.put_nowait(message)
                assert await asyncio.wait_for(upstream.outgoing.get(), 2) == message
            elif ending == "completed":
                message = {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": {"id": "turn"}},
                }
                upstream.incoming.put_nowait(message)
                assert await asyncio.wait_for(tui.outgoing.get(), 2) == message
            else:
                result = {"answers": {question["id"]: {"answers": ["Cancel"]}}}
                reply = (
                    {"id": prompt["id"], "result": result}
                    if ending == "cancel"
                    else {"id": prompt["id"], "error": {"code": -32601, "message": "unsupported"}}
                )
                tui.incoming.put_nowait(reply)
            resolved = await asyncio.wait_for(tui.outgoing.get(), 2)
            assert resolved == {
                "method": "serverRequest/resolved",
                "params": {"threadId": thread_id, "requestId": prompt["id"]},
            }
            reply = await asyncio.wait_for(upstream.outgoing.get(), 2)
            assert reply["id"] == "native-call"
            assert reply["result"]["success"] == (ending != "error")
            assert calls == (
                [{"action": "off"}]
                if ending == "error"
                else [{"action": "off"}, {"action": "cancel"}]
            )
            assert json.loads(controls.read_text()) == {}
            tui.incoming.put_nowait({"id": prompt["id"], "result": {"answers": {}}})
            sentinel = {"id": "after-stale", "method": "ping"}
            tui.incoming.put_nowait(sentinel)
            assert await asyncio.wait_for(upstream.outgoing.get(), 2) == sentinel
            assert upstream.outgoing.empty()

    asyncio.run(run())
