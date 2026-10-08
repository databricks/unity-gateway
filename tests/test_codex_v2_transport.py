from __future__ import annotations

import copy
import json

import pytest

from ucode.smart_routing.codex_v2_transport import (
    NATIVE_NAMESPACE,
    WIRE_NAMESPACE,
    prepare_request,
    transform_response_event,
)


def _message_schema(*, encrypted: bool = True) -> dict:
    message = {"type": "string", "description": "child assignment"}
    if encrypted:
        message["encrypted"] = True
    return {
        "type": "namespace",
        "name": NATIVE_NAMESPACE,
        "description": "native collaboration tools",
        "tools": [
            {
                "type": "function",
                "name": "spawn_agent",
                "description": "spawn",
                "parameters": {"type": "object", "properties": {"message": message}},
            },
            {
                "type": "function",
                "name": "send_message",
                "description": "send",
                "parameters": {"type": "object", "properties": {"message": copy.deepcopy(message)}},
            },
            {
                "type": "function",
                "name": "followup_task",
                "description": "follow up",
                "parameters": {"type": "object", "properties": {"message": copy.deepcopy(message)}},
            },
        ],
    }


def _request(**overrides: object) -> bytes:
    payload = {"model": "gpt-5.6-sol", "tools": [_message_schema()], "input": []}
    payload.update(overrides)
    return json.dumps(payload).encode()


def _decode(body: bytes) -> dict:
    return json.loads(body)


def test_prepare_request_rewrites_native_tools_and_plaintext_replay_marker():
    body, changed = prepare_request(
        _request(
            input=[
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "spawn_agent",
                    "namespace": NATIVE_NAMESPACE,
                    "arguments": '{"message":"full task"}',
                    "encrypted_function_args": [],
                }
            ]
        )
    )

    assert changed
    payload = _decode(body)
    namespace = payload["tools"][0]
    assert namespace["name"] == WIRE_NAMESPACE
    assert namespace["description"] == "native collaboration tools"
    for tool in namespace["tools"]:
        assert "encrypted" not in tool["parameters"]["properties"]["message"]
    replay = payload["input"][0]
    assert replay["namespace"] == WIRE_NAMESPACE
    assert "encrypted_function_args" not in replay
    assert replay["id"] == "fc-1"
    assert replay["call_id"] == "call-1"
    assert replay["arguments"] == '{"message":"full task"}'


@pytest.mark.parametrize("name", ["spawn_agent", "send_message", "followup_task"])
def test_prepare_request_removes_encryption_marker_for_each_plaintext_tool(name: str):
    namespace = _message_schema()
    namespace["tools"] = [
        namespace["tools"][[tool["name"] for tool in namespace["tools"]].index(name)]
    ]
    body, changed = prepare_request(_request(tools=[namespace]))

    assert changed
    tool = _decode(body)["tools"][0]["tools"][0]
    assert tool["name"] == name
    assert "encrypted" not in tool["parameters"]["properties"]["message"]


def test_prepare_request_handles_responses_lite_tools_and_output_replay():
    lite_namespace = _message_schema()
    original = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "additional_tools",
                "id": "at-1",
                "role": "developer",
                "tools": [lite_namespace],
            },
            {
                "type": "function_call_output",
                "id": "out-1",
                "call_id": "call-1",
                "namespace": NATIVE_NAMESPACE,
                "output": "child complete",
            },
        ],
    }

    body, changed = prepare_request(json.dumps(original).encode())
    payload = _decode(body)
    assert changed
    additional = payload["input"][0]
    assert additional["id"] == "at-1"
    assert additional["tools"][0]["name"] == WIRE_NAMESPACE
    output = payload["input"][1]
    assert output["namespace"] == WIRE_NAMESPACE
    assert output["output"] == "child complete"
    assert output["id"] == "out-1"


@pytest.mark.parametrize(
    "arguments,marker",
    [
        ('{"message":"task"}', {"encrypted_function_args": ["encrypted"]}),
        ('{"message":"gAAAAABopaque"}', {}),
    ],
)
def test_prepare_request_preserves_encrypted_history_and_matching_output(arguments, marker):
    history = [
        {
            "type": "function_call",
            "call_id": "old-call",
            "name": "spawn_agent",
            "namespace": NATIVE_NAMESPACE,
            "arguments": arguments,
            **marker,
        },
        {
            "type": "function_call_output",
            "call_id": "old-call",
            "namespace": NATIVE_NAMESPACE,
            "output": "child finished",
        },
    ]
    transformed, changed = prepare_request(_request(input=history))
    payload = _decode(transformed)
    assert changed
    assert payload["input"] == history
    assert payload["tools"][0]["name"] == WIRE_NAMESPACE
    for tool in payload["tools"][0]["tools"]:
        assert tool["parameters"]["properties"]["message"] == {
            "type": "string",
            "description": "child assignment",
        }


def test_prepare_request_preserves_additional_native_tools():
    namespace = _message_schema()
    namespace["tools"].append(
        {
            "type": "function",
            "name": "unknown_agent_tool",
            "parameters": {"type": "object", "properties": {}},
        }
    )
    transformed, changed = prepare_request(_request(tools=[namespace]))
    assert changed
    result = _decode(transformed)["tools"][0]
    assert result["name"] == WIRE_NAMESPACE
    assert result["tools"][-1] == namespace["tools"][-1]


def test_prepare_request_maps_additional_native_tool_replay():
    call = {
        "type": "function_call",
        "name": "unknown_agent_tool",
        "namespace": NATIVE_NAMESPACE,
        "arguments": "{}",
    }
    transformed, changed = prepare_request(_request(input=[call]))
    assert changed
    assert _decode(transformed)["input"] == [{**call, "namespace": WIRE_NAMESPACE}]


def test_prepare_request_leaves_namespace_literals_in_metadata_and_schemas():
    metadata = {"namespace": WIRE_NAMESPACE}
    tool = {"type": "function", "name": "other", "parameters": {"const": metadata}}
    transformed, changed = prepare_request(_request(metadata=metadata, tools=[tool]))
    assert not changed
    assert _decode(transformed)["metadata"] == metadata
    assert _decode(transformed)["tools"] == [tool]


def test_prepare_request_leaves_unrelated_tools_and_fields_unchanged():
    body = json.dumps(
        {
            "model": "gpt-5.6-sol",
            "metadata": {"request_id": "req-1"},
            "tools": [
                {
                    "type": "namespace",
                    "name": "other_tools",
                    "description": "unrelated",
                    "tools": [
                        {
                            "type": "function",
                            "name": "other",
                            "parameters": {
                                "properties": {"message": {"type": "string", "encrypted": True}}
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    transformed, changed = prepare_request(body)

    assert not changed
    assert transformed == body


def test_prepare_request_rejects_wire_namespace_collision():
    with pytest.raises(ValueError, match="reserved namespace"):
        prepare_request(
            _request(
                tools=[
                    {
                        "type": "namespace",
                        "name": WIRE_NAMESPACE,
                        "tools": [],
                    }
                ]
            )
        )


def test_prepare_request_does_not_set_native_namespace_configuration():
    original = {"features": {"multi_agent_v2": {"enabled": True}}, "tools": [_message_schema()]}
    body, changed = prepare_request(json.dumps(original).encode())

    payload = _decode(body)
    assert changed
    assert payload["features"] == original["features"]
    assert "tool_namespace" not in payload["features"]["multi_agent_v2"]


def test_transform_response_added_empty_arguments_and_done_full_arguments():
    added = transform_response_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "spawn_agent",
                "namespace": WIRE_NAMESPACE,
                "arguments": "",
            },
        }
    )
    assert added["item"]["namespace"] == NATIVE_NAMESPACE
    assert added["item"]["encrypted_function_args"] == []
    assert added["item"]["id"] == "item-1"
    assert added["item"]["call_id"] == "call-1"

    done = transform_response_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "spawn_agent",
                "namespace": WIRE_NAMESPACE,
                "arguments": '{"message":"Compute 137 * 29"}',
            },
        }
    )
    assert done["item"]["namespace"] == NATIVE_NAMESPACE
    assert done["item"]["encrypted_function_args"] == []
    assert done["item"]["arguments"] == '{"message":"Compute 137 * 29"}'


def test_transform_response_defers_added_argument_validation_until_done():
    added = transform_response_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "name": "send_message",
                "namespace": WIRE_NAMESPACE,
                "arguments": "not-final-json-yet",
            },
        }
    )
    assert added["item"]["namespace"] == NATIVE_NAMESPACE
    assert added["item"]["encrypted_function_args"] == []

    with pytest.raises(ValueError, match="valid JSON"):
        transform_response_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "send_message",
                    "namespace": WIRE_NAMESPACE,
                    "arguments": "not-final-json-yet",
                },
            }
        )


def test_transform_response_maps_full_response_output_and_preserves_ids():
    event = {
        "type": "response.completed",
        "response": {
            "id": "resp-1",
            "output": [
                {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "followup_task",
                    "namespace": WIRE_NAMESPACE,
                    "arguments": '{"message":"follow up"}',
                }
            ],
        },
    }
    transformed = transform_response_event(event)

    assert transformed["response"]["id"] == "resp-1"
    item = transformed["response"]["output"][0]
    assert item["namespace"] == NATIVE_NAMESPACE
    assert item["encrypted_function_args"] == []
    assert item["id"] == "item-1"
    assert item["call_id"] == "call-1"


def test_transform_response_rejects_encrypted_marker_and_opaque_message():
    encrypted = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "send_message",
            "namespace": WIRE_NAMESPACE,
            "arguments": '{"message":"not readable"}',
            "encrypted_function_args": ["ciphertext"],
        },
    }
    with pytest.raises(ValueError, match="encrypted function arguments"):
        transform_response_event(encrypted)

    opaque = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "send_message",
            "namespace": WIRE_NAMESPACE,
            "arguments": '{"message":"gAAAAABopaque"}',
        },
    }
    with pytest.raises(ValueError, match="opaque message"):
        transform_response_event(opaque)

    for arguments in ("", "[]", '{"message": 3}'):
        with pytest.raises(ValueError):
            transform_response_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "send_message",
                        "namespace": WIRE_NAMESPACE,
                        "arguments": arguments,
                    },
                }
            )


def test_transform_response_maps_additional_alias_tool():
    item = {
        "type": "function_call",
        "name": "unknown_agent_tool",
        "namespace": WIRE_NAMESPACE,
        "arguments": "{}",
    }
    result = transform_response_event({"type": "response.output_item.done", "item": item})
    assert result["item"] == {**item, "namespace": NATIVE_NAMESPACE}


def test_transform_response_rejects_native_collaboration_namespace():
    with pytest.raises(ValueError, match="native collaboration namespace"):
        transform_response_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "namespace": NATIVE_NAMESPACE,
                    "arguments": '{"message":"gAAAAABopaque"}',
                    "encrypted_function_args": ["ciphertext"],
                },
            }
        )

    with pytest.raises(ValueError, match="native collaboration namespace"):
        transform_response_event(
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "send_message",
                            "namespace": NATIVE_NAMESPACE,
                            "arguments": '{"message":"readable"}',
                            "encrypted_function_args": [],
                        }
                    ]
                },
            }
        )


@pytest.mark.parametrize("item_type", ["function_call_output", "message"])
def test_transform_response_rejects_non_function_call_alias_item(item_type: str):
    with pytest.raises(ValueError, match="not a function call"):
        transform_response_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": item_type,
                    "name": "spawn_agent",
                    "namespace": WIRE_NAMESPACE,
                    "arguments": '{"message":"readable"}',
                },
            }
        )


def test_transform_response_maps_known_nonplaintext_tools_without_plaintext_marker():
    transformed = transform_response_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "wait_agent",
                "namespace": WIRE_NAMESPACE,
                "arguments": '{"ids":["agent-1"]}',
            },
        }
    )
    assert transformed["item"]["namespace"] == NATIVE_NAMESPACE
    assert "encrypted_function_args" not in transformed["item"]


def test_transform_response_preserves_marker_for_nonmessage_tool():
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "id": "item-2",
            "name": "wait_agent",
            "namespace": WIRE_NAMESPACE,
            "arguments": "ciphertext",
            "encrypted_function_args": ["ciphertext"],
        },
    }

    transformed = transform_response_event(event)
    assert transformed["item"] == {**event["item"], "namespace": NATIVE_NAMESPACE}
