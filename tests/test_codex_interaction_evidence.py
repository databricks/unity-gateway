import pytest

from tests.integration.utils.agents.codex import (
    assert_agent_interaction_completed,
    completed_command_outputs,
)


def delivery_receipt(**changes):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "SubAgentActivity",
                "kind": "interacted",
                "agent_path": "/root/new__sr_off",
                "agent_thread_id": "child-id",
                **changes,
            },
        },
    }


def test_native_delivery_receipt_proves_interaction_without_direct_function_call():
    assert_agent_interaction_completed([delivery_receipt()], "/root/new__sr_off", "child-id")


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "started"},
        {"kind": "completed"},
        {"agent_path": "/root/another"},
        {"agent_thread_id": "different-child"},
        {"type": "CollabAgentToolCall"},
    ],
)
def test_unrelated_activity_does_not_prove_delivery(changes):
    with pytest.raises(AssertionError, match="No successful native interaction"):
        assert_agent_interaction_completed(
            [delivery_receipt(**changes)], "/root/new__sr_off", "child-id"
        )


def test_tool_invocation_without_delivery_receipt_does_not_prove_success():
    records = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "send_message",
                "arguments": '{"target":"/root/new__sr_off","message":"hello"}',
            },
        }
    ]
    with pytest.raises(AssertionError, match="No successful native interaction"):
        assert_agent_interaction_completed(records, "/root/new__sr_off", "child-id")


@pytest.mark.parametrize(
    ("status", "exit_code", "expected"),
    [("completed", 0, ["Smart Router is off"]), ("completed", 1, []), ("inProgress", 0, [])],
)
def test_command_confirmation_requires_native_success(status, exit_code, expected):
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "status": status,
                    "exit_code": exit_code,
                    "aggregated_output": "Smart Router is off",
                },
            },
        }
    ]
    assert completed_command_outputs(records) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('"$PYTHON" -m ucode.cli codex --disable-smart-routing', ["changed"]),
        (["/bin/zsh", "-lc", '"$PYTHON" -m ucode.cli codex --disable-smart-routing'], ["changed"]),
        ('"$PYTHON" -m ucode.cli codex --enable-smart-routing', []),
        ("echo changed", []),
        (None, []),
        (["python", {}], []),
    ],
)
def test_toggle_command_receipt_requires_the_requested_command(command, expected):
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "status": "completed",
                    "exit_code": 0,
                    "command": command,
                    "aggregated_output": "changed",
                },
            },
        }
    ]
    assert (
        completed_command_outputs(
            records, command_contains="-m ucode.cli codex --disable-smart-routing"
        )
        == expected
    )
