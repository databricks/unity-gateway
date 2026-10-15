"""Offline policy tests for the frozen Codex V10 Responses runtime."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from ucode.smart_routing.codex_runtime import (
    CodexRuntime,
    _ResponseTransformer,
    bound_owner_contract,
    inject_owner_contract,
)
from ucode.smart_routing.delegated_wait import eligible_wait, extend_collaboration_schema

SKILL = """---
name: smart-router-orchestrator
---
# Orchestration

<!-- DELEGATED_OWNER_CONTRACT_BEGIN -->
Every delegated owner executes its task through verified completion.
<!-- DELEGATED_OWNER_CONTRACT_END -->
"""
SKILL_HASH = hashlib.sha256(SKILL.encode()).hexdigest()


def activation(*, truncated: bool = True) -> dict:
    prefix = "Smart Router Orchestrator is on for this session.\nSkill directory: /opt/ucode/skills/smart-router-orchestrator/\n"
    if truncated:
        text = (
            "Warning: truncated output (original token count: 12000)\n"
            "Total output lines: 600\n\n" + prefix + SKILL[:45]
        )
    else:
        text = prefix + SKILL
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    }


def runtime(*, enabled=True) -> CodexRuntime:
    return CodexRuntime(SKILL, lambda: enabled)


def root_payload(
    *, input_items=None, stream=False, tools=None, cache_key=None, model="test-model"
) -> dict:
    payload = {
        "model": model,
        "reasoning_effort": "high",
        "input": [activation()] if input_items is None else input_items,
        "stream": stream,
    }
    if tools is not None:
        payload["tools"] = tools
    if cache_key is not None:
        payload["prompt_cache_key"] = cache_key
    return payload


def collaboration_tools(*, lite=False, max_timeout=3_600_000) -> list[dict]:
    declarations: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "task_name": {"type": "string"},
                    "target": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        }
        for name in ("spawn_agent", "followup_task")
    ]
    declarations.append(
        {
            "type": "function",
            "name": "wait_agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeout_ms": {
                        "type": "integer",
                        "maximum": max_timeout,
                        "description": f"Wait timeout, maximum {max_timeout:,} milliseconds.",
                    }
                },
                "required": ["timeout_ms"],
            },
        }
    )
    tools = [{"type": "namespace", "name": "collaboration", "tools": declarations}]
    return [{"type": "additional_tools", "tools": tools}] if lite else tools


def _all_tools(payload: dict) -> list[dict]:
    result = list(payload.get("tools", []))
    result.extend(
        tool
        for item in payload.get("input", [])
        if isinstance(item, dict) and item.get("type") == "additional_tools"
        for tool in item["tools"]
    )
    return result


def _response_item(
    *, hint: object = True, call_id="spawn-1", name="spawn_agent", arguments=None
) -> dict:
    call_arguments = {"task_name": "worker", "message": "Inspect source"}
    if hint is not ...:
        call_arguments["await_completion"] = hint
    if arguments is not None:
        call_arguments = arguments
    return {
        "type": "function_call",
        "id": "fc-1",
        "call_id": call_id,
        "namespace": "ucode_collaboration",
        "name": name,
        "arguments": json.dumps(call_arguments, separators=(",", ":")),
    }


def _successful_spawn_history(item: dict | None = None, *, owner="/root/worker") -> list[dict]:
    spawn = item or {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "spawn-1",
        "namespace": "collaboration",
        "name": "spawn_agent",
        "arguments": '{"task_name":"worker","message":"Inspect source"}',
    }
    return [
        spawn,
        {
            "type": "function_call_output",
            "call_id": spawn["call_id"],
            "output": json.dumps({"task_name": owner}),
        },
    ]


def _wait_request(runtime_instance: CodexRuntime, *, stream=False, owner="/root/worker"):
    spawn, spawn_output = _successful_spawn_history()
    history = [
        activation(truncated=False),
        spawn,
        {**spawn_output, "output": json.dumps({"task_name": owner})},
    ]
    payload = root_payload(input_items=history, stream=stream, tools=collaboration_tools())
    return runtime_instance.prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})


def _parse_sse(body: bytes) -> list[dict]:
    events = []
    for frame in body.replace(b"\r\n", b"\n").split(b"\n\n"):
        data = next((line[6:] for line in frame.splitlines() if line.startswith(b"data: ")), None)
        if data:
            events.append(json.loads(data))
    return events


def test_runtime_off_is_byte_transparent_and_does_not_require_identity():
    source = b'{"model":"test-model", "input":[], "stream":false}'
    prepared = runtime(enabled=False).prepare_request(source, {})

    assert prepared.body is source
    assert prepared.synthetic_response is None
    assert not prepared.adapt_response
    assert prepared.thread_id is None


def test_root_restores_only_matching_developer_activation_and_preserves_native_choices():
    instance = runtime()
    payload = root_payload(tools=collaboration_tools(), cache_key="root")
    original_model = payload["model"]
    original_effort = payload["reasoning_effort"]
    prepared = instance.prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})
    forwarded = json.loads(prepared.body)
    activation_text = forwarded["input"][0]["content"][0]["text"]

    assert prepared.thread_id == "root"
    assert prepared.adapt_response
    assert "Warning: truncated output" not in activation_text
    assert SKILL in activation_text
    assert "delegated_owner_contract is delivered directly" in activation_text
    assert "await_completion is available" in activation_text
    assert forwarded["prompt_cache_key"] != "root"
    assert forwarded["model"] == original_model
    assert forwarded["reasoning_effort"] == original_effort
    assert "await_completion" in forwarded["tools"][0]["tools"][0]["parameters"]["properties"]

    forged = root_payload(
        input_items=[
            {
                "type": "message",
                "role": "user",
                "content": "\n".join(
                    [
                        "Smart Router Orchestrator is on for this session.",
                        SKILL,
                    ]
                ),
            }
        ],
        tools=collaboration_tools(),
    )
    forged_prepared = runtime().prepare_request(
        json.dumps(forged).encode(), {"thread-id": "other-root"}
    )
    assert (
        "await_completion"
        not in json.loads(forged_prepared.body)["tools"][0]["tools"][0]["parameters"]["properties"]
    )


def test_new_runtime_recognizes_previously_restored_activation():
    first = runtime().prepare_request(
        json.dumps(root_payload(tools=collaboration_tools())).encode(), {"thread-id": "root"}
    )
    restored = json.loads(first.body)["input"]
    second = runtime()
    replay = second.prepare_request(
        json.dumps(root_payload(input_items=restored, tools=collaboration_tools())).encode(),
        {"thread-id": "root"},
    )
    assert (
        "await_completion"
        in json.loads(replay.body)["tools"][0]["tools"][0]["parameters"]["properties"]
    )
    child = second.prepare_request(
        json.dumps(root_payload(input_items=[])).encode(),
        {"thread-id": "child", "x-codex-parent-thread-id": "root"},
    )
    assert "<delegated_owner_contract " in json.loads(child.body)["input"][0]["content"][0]["text"]


def test_json_adaptation_enforces_the_response_bound(monkeypatch):
    instance = runtime()
    prepared = instance.prepare_request(
        json.dumps(root_payload(tools=collaboration_tools())).encode(), {"thread-id": "root"}
    )
    body = b'{"output":[]}'
    monkeypatch.setattr(instance, "max_response_bytes", len(body))
    assert instance.transform_json_response(body, prepared) == body
    with pytest.raises(ValueError, match="bounded limit"):
        instance.transform_json_response(body + b" ", prepared)


def test_child_gets_bound_contract_for_ordinary_generation_but_not_compaction():
    instance = runtime()
    instance.prepare_request(json.dumps(root_payload()).encode(), {"thread-id": "root"})

    child = {
        "model": "test-model",
        "input": [{"type": "message", "role": "user", "content": "inspect"}],
    }
    prepared = instance.prepare_request(
        json.dumps(child).encode(),
        {
            "thread-id": "child",
            "x-codex-parent-thread-id": "root",
        },
    )
    forwarded = json.loads(prepared.body)
    contract = forwarded["input"][0]["content"][0]["text"]
    assert contract.startswith("<delegated_owner_contract sha256=")
    assert "Every delegated owner executes" in contract

    compact = {
        **child,
        "client_metadata": {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})},
    }
    compact_prepared = instance.prepare_request(
        json.dumps(compact).encode(),
        {
            "thread-id": "child",
            "x-codex-parent-thread-id": "root",
        },
    )
    assert "delegated_owner_contract" not in compact_prepared.body.decode()


def test_stale_duplicate_or_unhashable_owner_contracts_fail_closed():
    contract = bound_owner_contract({"body": SKILL, "sha256": SKILL_HASH})
    assert contract is not None
    existing = {"input": [{"role": "developer", "content": [{"text": contract}]}]}
    assert not inject_owner_contract(existing, contract)

    stale = {
        "input": [
            {
                "role": "developer",
                "content": [
                    {
                        "text": '<delegated_owner_contract sha256="old">old</delegated_owner_contract>'
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="stale or duplicate"):
        inject_owner_contract(stale, contract)
    duplicate = {
        "input": [{"role": "developer", "content": [{"text": contract}, {"text": contract}]}]
    }
    with pytest.raises(ValueError, match="stale or duplicate"):
        inject_owner_contract(duplicate, contract)
    with pytest.raises(ValueError, match="hash-bound"):
        bound_owner_contract({"body": SKILL, "sha256": "wrong"})


@pytest.mark.parametrize("lite", [False, True])
def test_wait_hint_schema_extends_native_and_responses_lite_tools(lite):
    payload = {"input": [{"type": "message", "role": "user", "content": "work"}]}
    tools = collaboration_tools(lite=lite)
    if lite:
        payload["input"].extend(tools)
    else:
        payload["tools"] = tools

    assert extend_collaboration_schema(payload)
    declarations = _all_tools(payload)
    spawn = declarations[0]["tools"][0]
    assert spawn["parameters"]["properties"]["await_completion"]["type"] == "boolean"
    assert "await_completion" not in spawn["parameters"]["required"]
    assert not extend_collaboration_schema(payload)


def test_encrypted_collaboration_replay_stays_native_while_lite_schema_is_translated():
    encrypted_call = {
        "type": "function_call",
        "id": "fc-encrypted",
        "call_id": "encrypted-1",
        "namespace": "collaboration",
        "name": "send_message",
        "arguments": '{"target":"/root/worker","message":"gAAAAAopaque"}',
        "encrypted_function_args": ["opaque-ciphertext"],
    }
    history = [
        activation(truncated=False),
        encrypted_call,
        {"type": "function_call_output", "call_id": "encrypted-1", "output": "sent"},
    ]
    payload = root_payload(input_items=history)
    payload["input"].extend(collaboration_tools(lite=True))
    prepared = runtime().prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})
    forwarded = json.loads(prepared.body)

    assert forwarded["input"][1] == encrypted_call
    assert forwarded["input"][2]["call_id"] == "encrypted-1"
    assert forwarded["input"][2]["type"] == "function_call_output"
    declarations = forwarded["input"][3]["tools"]
    assert declarations[0]["name"] == "ucode_collaboration"


def test_unsupported_await_schema_and_non_boolean_stream_fail_closed():
    bad = {"tools": collaboration_tools(), "input": [activation(truncated=False)]}
    bad["tools"][0]["tools"][0]["parameters"]["properties"]["await_completion"] = None
    with pytest.raises(ValueError, match="unsupported existing"):
        runtime().prepare_request(json.dumps(bad).encode(), {"thread-id": "root"})

    payload = root_payload(stream=1)
    with pytest.raises(ValueError, match="stream option"):
        runtime().prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})


@pytest.mark.parametrize("hint", [True, False, ...])
@pytest.mark.parametrize("stream", [False, True])
def test_wait_hint_is_stripped_and_only_true_declaration_synthesizes_wait(hint, stream):
    instance = runtime()
    payload = root_payload(tools=collaboration_tools())
    first = instance.prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})
    response = {"output": [_response_item(hint=hint)], "usage": {"output_tokens": 5}}
    transformed = instance.transform_json_response(json.dumps(response).encode(), first)
    call = json.loads(transformed)["output"][0]
    assert "await_completion" not in json.loads(call["arguments"])
    assert "usage" in json.loads(transformed)

    followup = _wait_request(instance, stream=stream)
    if hint is True:
        assert followup.synthetic_response is not None
        assert followup.synthetic_content_type == (
            "text/event-stream" if stream else "application/json"
        )
        if stream:
            events = _parse_sse(followup.synthetic_response)
            wait = events[-1]["response"]["output"][0]
        else:
            wait = json.loads(followup.synthetic_response)["output"][0]
        assert wait["namespace"] == "collaboration"
        assert wait["name"] == "wait_agent"
        assert json.loads(wait["arguments"]) == {"timeout_ms": 3_600_000}
    else:
        assert followup.synthetic_response is None


def test_wait_binding_requires_actual_unique_owner_and_no_intervening_message():
    wait_tools = collaboration_tools()
    payload = {"input": _successful_spawn_history()}
    followup = {
        "type": "function_call",
        "call_id": "followup-1",
        "namespace": "collaboration",
        "name": "followup_task",
        "arguments": '{"target":"/root/worker","message":"finish"}',
    }
    payload["input"].extend(
        [
            followup,
            {
                "type": "function_call_output",
                "call_id": "followup-1",
                "output": '{"task_name":"/root/worker"}',
            },
        ]
    )
    payload["tools"] = wait_tools
    binding = eligible_wait(payload, {"followup-1": True})
    assert binding is not None
    assert binding["owner"] == "/root/worker"
    assert eligible_wait(payload, {"followup-1": False}) is None
    assert eligible_wait(payload, {}) is None
    for failure in ({"error": "rejected"}, {"ok": False}, {"success": False}):
        failed = {
            **payload,
            "input": [
                *payload["input"][:-1],
                {
                    **payload["input"][-1],
                    "output": json.dumps({"task_name": "/root/worker", **failure}),
                },
            ],
        }
        assert eligible_wait(failed, {"followup-1": True}) is None

    for interrupt in (
        {"type": "message", "role": "user", "content": "new request"},
        {"type": "message", "role": "developer", "content": "new constraint"},
        {"type": "agent_message", "author": "/root/worker", "content": "progress"},
        {"type": "message", "role": "agent", "content": "progress"},
    ):
        interrupted = {
            **payload,
            "input": [*payload["input"][:-1], interrupt, payload["input"][-1]],
        }
        assert eligible_wait(interrupted, {"followup-1": True}) is None

    second_owner = _successful_spawn_history(
        {
            "type": "function_call",
            "call_id": "spawn-2",
            "namespace": "collaboration",
            "name": "spawn_agent",
            "arguments": '{"task_name":"other","message":"work"}',
        },
        owner="/root/other",
    )
    multiple = {**payload, "input": [*payload["input"][:2], *second_owner, *payload["input"][2:]]}
    assert eligible_wait(multiple, {"followup-1": True}) is None

    bad_return = _successful_spawn_history(owner="/root/forged")
    mismatched = {**payload, "input": [*bad_return, *payload["input"][2:]]}
    assert eligible_wait(mismatched, {"followup-1": True}) is None

    wrong_wait_limit = {
        "input": payload["input"],
        "tools": collaboration_tools(max_timeout=300_000),
    }
    assert eligible_wait(wrong_wait_limit, {"followup-1": True}) is None


def test_wait_checks_earlier_followup_owners_as_well_as_spawns():
    for earlier_owner in ("worker", "other"):
        history = _successful_spawn_history()
        for call_id, owner in (("earlier-followup", earlier_owner), ("final-followup", "worker")):
            history.extend(
                [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "namespace": "collaboration",
                        "name": "followup_task",
                        "arguments": json.dumps({"target": owner, "message": "finish"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": "/root/" + owner}),
                    },
                ]
            )
        binding = eligible_wait(
            {"input": history, "tools": collaboration_tools()}, {"final-followup": True}
        )
        assert (binding is not None) == (earlier_owner == "worker")


def test_wait_flags_are_isolated_by_native_thread_and_parent_mismatches_reject():
    instance = runtime()
    root = root_payload(tools=collaboration_tools())
    first = instance.prepare_request(json.dumps(root).encode(), {"thread-id": "root-a"})
    item = _response_item()
    instance.transform_json_response(json.dumps({"output": [item]}).encode(), first)

    other = root_payload(
        input_items=[activation(truncated=False), *_successful_spawn_history()],
        tools=collaboration_tools(),
    )
    second = instance.prepare_request(json.dumps(other).encode(), {"thread-id": "root-b"})
    assert second.synthetic_response is None

    mismatch = root_payload()
    mismatch["client_metadata"] = {"thread_id": "other"}
    with pytest.raises(ValueError, match="thread identity"):
        instance.prepare_request(json.dumps(mismatch).encode(), {"thread-id": "root-a"})
    mismatch["client_metadata"] = {"thread_id": "root-a", "x-codex-parent-thread-id": "parent-a"}
    with pytest.raises(ValueError, match="parent identity"):
        instance.prepare_request(
            json.dumps(mismatch).encode(),
            {
                "thread-id": "root-a",
                "x-codex-parent-thread-id": "parent-b",
            },
        )


def test_custom_cache_keys_are_untouched_and_default_keys_follow_tool_profile():
    instance = runtime()
    custom = root_payload(tools=collaboration_tools(), cache_key="caller-managed")
    custom_out = json.loads(
        instance.prepare_request(json.dumps(custom).encode(), {"thread-id": "root"}).body
    )
    assert custom_out["prompt_cache_key"] == "caller-managed"

    first = root_payload(tools=collaboration_tools(), cache_key="root")
    first_key = json.loads(
        instance.prepare_request(json.dumps(first).encode(), {"thread-id": "root"}).body
    )["prompt_cache_key"]
    changed_tools = collaboration_tools()
    changed_tools[0]["tools"][0]["parameters"]["properties"]["task_name"]["description"] = (
        "Different schema profile"
    )
    second = root_payload(tools=changed_tools, cache_key="root")
    second_key = json.loads(
        instance.prepare_request(json.dumps(second).encode(), {"thread-id": "root"}).body
    )["prompt_cache_key"]
    assert first_key != second_key


def test_sse_buffers_split_crlf_arguments_preserves_other_events_and_strips_hint():
    instance = runtime()
    prepared = instance.prepare_request(
        json.dumps(root_payload(tools=collaboration_tools(), stream=True)).encode(),
        {"thread-id": "root"},
    )
    transformer = instance.response_transformer(prepared)
    args = '{"task_name":"worker","message":"Inspect source","await_completion":true}'
    item = _response_item(hint=True)
    item["arguments"] = ""
    events: list[dict[str, Any]] = [
        {
            "type": "response.reasoning_summary_text.delta",
            "delta": "reasoning",
            "custom": {"keep": True},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": args[:33]},
        {"type": "response.function_call_arguments.delta", "item_id": "fc-1", "delta": args[33:]},
        {"type": "response.function_call_arguments.done", "item_id": "fc-1", "arguments": args},
        {"type": "response.output_item.done", "item": {**item, "arguments": args}},
        {
            "type": "response.completed",
            "response": {"output": [{**item, "arguments": args}], "usage": {"output_tokens": 7}},
        },
    ]
    wire = b"".join(
        b"event: "
        + event["type"].encode()
        + b"\r\ndata: "
        + json.dumps(event).encode()
        + b"\r\n\r\n"
        for event in events
    )
    result = []
    for byte in wire:
        result.extend(transformer.feed(bytes([byte])))
    result.extend(transformer.finish())
    output = _parse_sse(b"".join(result))

    assert output[0]["delta"] == "reasoning"
    assert output[0]["custom"] == {"keep": True}
    assert output[1]["item"]["namespace"] == "collaboration"
    assert output[1]["item"]["arguments"] == ""
    assert output[2]["delta"] == output[3]["delta"] == ""
    assert "await_completion" not in json.loads(output[4]["arguments"])
    assert "await_completion" not in json.loads(output[5]["item"]["arguments"])
    assert "await_completion" not in json.loads(output[6]["response"]["output"][0]["arguments"])
    assert output[6]["response"]["usage"] == {"output_tokens": 7}


def test_sse_malformed_expected_event_and_truncated_final_frame_raise():
    transformer = _ResponseTransformer(None, None, False)
    assert transformer.feed(b"data: arbitrary passthrough\n\n")

    instance = runtime()
    adapted = instance.prepare_request(
        json.dumps(root_payload(tools=collaboration_tools(), stream=True)).encode(),
        {"thread-id": "root"},
    )
    transformer = instance.response_transformer(adapted)
    with pytest.raises(ValueError, match="malformed"):
        transformer.feed(b"event: response.output_item.done\ndata: not-json\n\n")

    truncated = instance.response_transformer(adapted)
    assert truncated.feed(b'event: response.completed\ndata: {"type":"response.completed"') == []
    with pytest.raises(ValueError, match="mid-event"):
        truncated.finish()


def test_active_malformed_json_duplicate_fields_and_reserved_wire_namespace_reject():
    instance = runtime()
    for source in (b"not-json", b'{"model":"a","model":"b","input":[]}'):
        with pytest.raises(ValueError):
            instance.prepare_request(source, {"thread-id": "root"})
    malformed_tools = root_payload()
    malformed_tools["tools"] = {"type": "namespace"}
    with pytest.raises(ValueError, match="native tools must be a list"):
        instance.prepare_request(json.dumps(malformed_tools).encode(), {"thread-id": "root"})
    malformed_lite = root_payload(
        input_items=[activation(truncated=False), {"type": "additional_tools", "tools": None}]
    )
    with pytest.raises(ValueError, match="additional_tools"):
        instance.prepare_request(json.dumps(malformed_lite).encode(), {"thread-id": "root"})
    payload = root_payload(
        tools=[{"type": "namespace", "name": "ucode_collaboration", "tools": []}]
    )
    with pytest.raises(ValueError, match="reserved namespace"):
        instance.prepare_request(json.dumps(payload).encode(), {"thread-id": "root"})
