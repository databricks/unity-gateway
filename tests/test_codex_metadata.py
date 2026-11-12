"""Native metadata attribution and main-turn handoff behavior."""

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import session_env
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


def test_turn_metadata_preserves_other_fields_and_does_not_modify_child_input(tmp_path):
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
                    "responsesapiClientMetadataPatch": {"other": "kept"},
                },
            }
            forwarded = await metadata.before_tui(copy.deepcopy(message))
            expected = copy.deepcopy(message)
            expected["params"]["responsesapiClientMetadataPatch"][ROUTING_STATE_KEY] = (
                "on" if enabled else "off"
            )
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
def test_toggle_interrupts_only_main_and_preserves_conversation_tree(tmp_path, queued, cancel):
    async def run():
        metadata, upstream, tui, controls = controller(tmp_path)
        metadata.roots["main"] = RootTurn(active="main-turn")
        metadata.children.add("child")
        child_turn = {
            "method": "turn/started",
            "params": {"threadId": "child", "turn": {"id": "child-turn"}},
        }
        assert metadata.observe(child_turn) is False
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))
        if queued:
            metadata.buffer_input(
                {
                    "id": "user-start",
                    "method": "turn/start",
                    "params": {"threadId": "main", "input": [{"type": "text", "text": "new work"}]},
                }
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
                assert message["method"] == "turn/start"
                result = {"turn": {"id": "continued"}}
            metadata.observe({"id": message["id"], "result": result})

        upstream.on_send = reply
        if cancel:
            metadata.client_idle.clear()
            task = asyncio.create_task(metadata.refresh())
            await asyncio.sleep(0)
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
            metadata.client_idle.set()
            await task
            assert tui.messages == [{"id": "escape", "result": {}}]
        else:
            await metadata.refresh()
        assert "main" in metadata.roots and "child" in metadata.children
        assert all(message["params"]["threadId"] == "main" for message in upstream.messages)
        assert all(
            message["method"] not in {"thread/unsubscribe", "thread/resume"}
            for message in upstream.messages
        )
        starts = [message for message in upstream.messages if message["method"] == "turn/start"]
        assert len(starts) == (0 if queued or cancel else 1)
        if starts:
            assert starts[0]["params"]["responsesapiClientMetadataPatch"] == {
                ROUTING_STATE_KEY: "off"
            }
            assert "Do not restart or redelegate" in starts[0]["params"]["input"][0]["text"]
        if queued and not cancel:
            assert "main" in metadata.continuations

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
        metadata.cancelled.add("main")

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
