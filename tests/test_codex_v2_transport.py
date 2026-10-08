"""Regression cases for readable native assignments and encrypted-session replay."""

import json

import pytest

from ucode.smart_routing.codex_v2_transport import (
    NATIVE_NAMESPACE,
    WIRE_NAMESPACE,
    prepare_request,
    transform_response_event,
)

ASSIGNMENT = "Read résumé.txt.\nPreserve punctuation (!?;:)."
CALL = {
    "type": "function_call",
    "namespace": NATIVE_NAMESPACE,
    "name": "spawn_agent",
    "id": "item-1",
    "call_id": "call-1",
    "arguments": json.dumps({"message": ASSIGNMENT}),
    "encrypted_function_args": [],
}


@pytest.mark.parametrize(
    "layout,name",
    [
        ("namespace", "spawn_agent"),
        ("additional_tools", "send_message"),
        ("flat", "followup_task"),
        ("namespace", "future_tool"),
        ("flat", "future_tool"),
    ],
)
def test_request_changes_only_native_assignment_schemas(layout, name):
    function = {
        "type": "function",
        "name": name,
        "parameters": {"properties": {"message": {"type": "string", "encrypted": True}}},
    }
    tools = (
        [{**function, "namespace": NATIVE_NAMESPACE}]
        if layout == "flat"
        else [{"type": "namespace", "name": NATIVE_NAMESPACE, "tools": [function]}]
    )
    unrelated = {"type": "function", "name": "other", "parameters": {"const": WIRE_NAMESPACE}}
    tools.append(unrelated)
    request = {"tools": tools, "metadata": {"namespace": WIRE_NAMESPACE}}
    if layout == "additional_tools":
        request["input"] = [{"type": "additional_tools", "tools": request.pop("tools")}]

    body, changed = prepare_request(json.dumps(request).encode())
    result = json.loads(body)
    rewritten = result["input"][0]["tools"] if layout == "additional_tools" else result["tools"]
    assert changed
    assert result["metadata"] == request["metadata"]
    assert rewritten[1] == unrelated
    assert rewritten[0]["namespace" if layout == "flat" else "name"] == WIRE_NAMESPACE
    schema = rewritten[0] if layout == "flat" else rewritten[0]["tools"][0]
    expected_message = {"type": "string", **({"encrypted": True} if name == "future_tool" else {})}
    assert schema == {
        **function,
        **({"namespace": WIRE_NAMESPACE} if layout == "flat" else {}),
        "parameters": {"properties": {"message": expected_message}},
    }


@pytest.mark.parametrize(
    "arguments,marker",
    [('{"message":"gAAAAABopaque"}', []), ('{"message":"task"}', ["encrypted"])],
)
def test_replay_maps_plaintext_but_preserves_encrypted_calls_and_outputs(arguments, marker):
    output = {"type": "function_call_output", "namespace": NATIVE_NAMESPACE, "call_id": "call-1"}
    encrypted = {
        **CALL,
        "call_id": "old-call",
        "arguments": arguments,
        "encrypted_function_args": marker,
    }
    encrypted_output = {**output, "call_id": "old-call", "output": "done"}
    body, changed = prepare_request(
        json.dumps({"input": [CALL, output, encrypted, encrypted_output]}).encode()
    )
    plain_call = {key: value for key, value in CALL.items() if key != "encrypted_function_args"}
    assert changed
    assert json.loads(body)["input"] == [
        {**plain_call, "namespace": WIRE_NAMESPACE},
        {**output, "namespace": WIRE_NAMESPACE},
        encrypted,
        encrypted_output,
    ]


@pytest.mark.parametrize(
    "body", [b"invalid JSON", b"[]", b'{"tools":[]}', b'{"input":"unchanged"}']
)
def test_non_v2_requests_are_byte_transparent(body):
    assert prepare_request(body) == (body, False)


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "namespace", "name": WIRE_NAMESPACE},
        {"type": "function", "namespace": WIRE_NAMESPACE, "name": "other"},
    ],
)
def test_reserved_namespace_collision_is_rejected(tool):
    with pytest.raises(ValueError, match="reserved namespace"):
        prepare_request(json.dumps({"tools": [tool]}).encode())


@pytest.mark.parametrize(
    "name,shape",
    [
        ("spawn_agent", "added"),
        ("spawn_agent", "done"),
        ("send_message", "done"),
        ("followup_task", "completed"),
        ("spawn_agent", "json"),
        ("future_tool", "done"),
    ],
)
def test_response_restores_native_calls_without_changing_arguments_or_ids(name, shape):
    item = {**CALL, "name": name, "namespace": WIRE_NAMESPACE}
    del item["encrypted_function_args"]
    if shape == "added":
        item["arguments"] = '{"message":'
    if name == "future_tool":
        item.update(arguments="opaque non-assignment data", encrypted_function_args=["encrypted"])
    if shape in {"added", "done"}:
        event = {"type": "response.output_item." + shape, "item": item}
        expected_item = {**item, "namespace": NATIVE_NAMESPACE}
        expected = {**event, "item": expected_item}
    else:
        response = {"id": "response-1", "output": [item]}
        expected_item = {**item, "namespace": NATIVE_NAMESPACE}
        expected_response = {**response, "output": [expected_item]}
        event = {"response": response} if shape == "completed" else response
        expected = {"response": expected_response} if shape == "completed" else expected_response
    if name != "future_tool":
        expected_item["encrypted_function_args"] = []
    assert transform_response_event(event) == expected
    assert item["namespace"] == WIRE_NAMESPACE


@pytest.mark.parametrize(
    "override",
    [
        {"encrypted_function_args": ["encrypted"]},
        {"encrypted_function_args": None},
        {"arguments": '{"message":"gAAAAABopaque"}'},
        {"arguments": "gAAAAABopaque"},
        {"arguments": ""},
        {"arguments": "[]"},
        {"arguments": '{"message":3}'},
        {"namespace": NATIVE_NAMESPACE},
        {"type": "function_call_output"},
    ],
)
def test_completed_assignment_rejects_opaque_or_malformed_calls(override):
    item = {**CALL, "namespace": WIRE_NAMESPACE, **override}
    with pytest.raises(ValueError):
        transform_response_event({"type": "response.output_item.done", "item": item})
