"""Exercise the real loopback relay; only the external gateway transport is simulated."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from ucode import gateway_proxy
from ucode.smart_routing.codex_runtime import CodexRuntime


def activation_message(skill):
    return {
        "type": "message",
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": "Smart Router Orchestrator is on for this session. Apply the workflow below.\nSkill directory: "
                + str(skill.parent)
                + "\n\n"
                + skill.read_text(),
            }
        ],
    }


def json_response(payload):
    return httpx.Response(
        200,
        stream=httpx.ByteStream(json.dumps(payload).encode()),
        headers={"content-type": "application/json"},
    )


@contextmanager
def relay(respond, *, enabled=lambda: True):
    skill = Path(__file__).parents[1] / "skills/smart-router-orchestrator/SKILL.codex.md"
    runtime = CodexRuntime(skill.read_text(), enabled=enabled)
    # A child belongs to an already activated native parent in a real UG launch.
    runtime.prepare_request(
        json.dumps({"model": "test-model", "input": [activation_message(skill)]}).encode(),
        {"thread-id": "root"},
    )
    server, cache, original_client = gateway_proxy.start_codex_proxy(
        "https://workspace.example/ai-gateway/codex/v1", lambda force: "test-token", runtime
    )
    client = httpx.Client(
        base_url="https://workspace.example/ai-gateway/codex/v1/",
        transport=httpx.MockTransport(respond),
    )
    server.RequestHandlerClass.client = client
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        cache.stop()
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        client.close()
        original_client.close()


def child_request():
    return {
        "model": "test-model",
        "input": [{"role": "user", "content": "inspect"}],
        "stream": False,
        "tools": [
            {
                "type": "namespace",
                "name": "collaboration",
                "tools": [
                    {
                        "type": "function",
                        "name": "wait_agent",
                        "parameters": {
                            "type": "object",
                            "properties": {"timeout_ms": {"type": "integer", "maximum": 3600000}},
                        },
                    }
                ],
            }
        ],
    }


def test_relay_injects_contract_and_preserves_routing_headers_and_usage():
    received = []

    def respond(request):
        received.append(request)
        return json_response(
            {
                "object": "response",
                "output": [],
                "usage": {"input_tokens": 9, "output_tokens": 3},
            }
        )

    with relay(respond) as url:
        response = httpx.post(
            url + "/responses",
            json=child_request(),
            headers={
                "thread-id": "child",
                "x-codex-parent-thread-id": "root",
                "Databricks-Smart-Router-Recipe": "existing-recipe",
                "Authorization": "Bearer stale",
            },
        )

    assert response.status_code == 200
    assert response.json()["usage"] == {"input_tokens": 9, "output_tokens": 3}
    (request,) = received
    assert str(request.url) == "https://workspace.example/ai-gateway/codex/v1/responses"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["Databricks-Smart-Router-Recipe"] == "existing-recipe"
    injected = json.loads(request.content)["input"][0]
    assert injected["role"] == "developer"
    assert "<delegated_owner_contract " in injected["content"][0]["text"]


def test_disabled_runtime_is_byte_transparent():
    source = b'{"model":"test-model", "input":[], "stream":false}'
    received = []
    upstream = b'{"output":[], "usage":{"input_tokens":7}}'

    def respond(request):
        received.append(request.content)
        return httpx.Response(
            200, stream=httpx.ByteStream(upstream), headers={"content-type": "application/json"}
        )

    with relay(respond, enabled=lambda: False) as url:
        response = httpx.post(url + "/responses", content=source)

    assert received == [source]
    assert response.content == upstream


@pytest.mark.parametrize("status", [400, 429, 500])
def test_upstream_errors_keep_status_and_body(status):
    with relay(
        lambda request: httpx.Response(status, stream=httpx.ByteStream(b"original error"))
    ) as url:
        response = httpx.post(
            url + "/responses",
            json=child_request(),
            headers={
                "thread-id": "child",
                "x-codex-parent-thread-id": "root",
            },
        )
    assert response.status_code == status
    assert response.content == b"original error"


def test_oversized_adapted_json_is_rejected_before_reading_the_whole_stream(monkeypatch):
    monkeypatch.setattr(CodexRuntime, "max_response_bytes", 64)
    reads = []

    class OversizedBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append("first")
            yield b" " * (64 * 1024)
            reads.append("unread")
            yield b" " * (64 * 1024)

    with relay(
        lambda request: httpx.Response(
            200, stream=OversizedBody(), headers={"content-type": "application/json"}
        )
    ) as url:
        response = httpx.post(
            url + "/responses",
            json=child_request(),
            headers={"thread-id": "child", "x-codex-parent-thread-id": "root"},
        )
    assert response.status_code == 502
    assert reads == ["first"]


def test_invalid_active_request_does_not_reach_gateway():
    received = []
    with relay(lambda request: received.append(request)) as url:
        response = httpx.post(url + "/responses", content=b"not json")
    assert response.status_code == 400
    assert received == []


def test_compaction_keeps_the_gateway_route_and_omits_child_contract():
    received = []

    def respond(request):
        received.append(request)
        return json_response({"object": "response.compaction", "output": []})

    body = child_request()
    body["client_metadata"] = {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}
    with relay(respond) as url:
        response = httpx.post(
            url + "/responses/compact",
            json=body,
            headers={
                "thread-id": "child",
                "x-codex-parent-thread-id": "root",
            },
        )
    assert response.status_code == 200
    (request,) = received
    assert request.url.path == "/ai-gateway/codex/v1/responses/compact"
    assert "delegated_owner_contract" not in request.content.decode()


def test_stream_translates_namespace_and_keeps_usage_and_reasoning():
    item = {
        "type": "function_call",
        "id": "fc1",
        "call_id": "call1",
        "namespace": "ucode_collaboration",
        "name": "wait_agent",
        "arguments": '{"timeout_ms":3600000}',
    }
    events = [
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": "response1",
                "output": [item],
                "usage": {"output_tokens": 4, "output_tokens_details": {"reasoning_tokens": 2}},
            },
        },
    ]
    body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    with relay(
        lambda request: httpx.Response(
            200, stream=httpx.ByteStream(body), headers={"content-type": "text/event-stream"}
        )
    ) as url:
        response = httpx.post(
            url + "/responses",
            json=child_request(),
            headers={
                "thread-id": "child",
                "x-codex-parent-thread-id": "root",
            },
        )
    assert response.status_code == 200
    output = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert output[0]["item"]["namespace"] == "collaboration"
    assert output[1]["response"]["usage"]["output_tokens_details"]["reasoning_tokens"] == 2


def test_truncated_stream_is_an_explicit_error():
    with relay(
        lambda request: httpx.Response(
            200,
            stream=httpx.ByteStream(b'data: {"type":"response.'),
            headers={"content-type": "text/event-stream"},
        )
    ) as url:
        response = httpx.post(
            url + "/responses",
            json=child_request(),
            headers={
                "thread-id": "child",
                "x-codex-parent-thread-id": "root",
            },
        )
    assert "codex_runtime_protocol_error" in response.text
    assert "response.completed" not in response.text


@pytest.mark.parametrize("stream", [False, True])
def test_bound_wait_avoids_a_second_gateway_request(stream):
    skill = Path(__file__).parents[1] / "skills/smart-router-orchestrator/SKILL.codex.md"
    activation = activation_message(skill)
    tools = [
        {
            "type": "namespace",
            "name": "collaboration",
            "tools": [
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_name": {"type": "string"},
                            "message": {"type": "string"},
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "wait_agent",
                    "parameters": {
                        "type": "object",
                        "properties": {"timeout_ms": {"type": "integer", "maximum": 3600000}},
                    },
                },
            ],
        }
    ]
    spawn = {
        "type": "function_call",
        "id": "fc_spawn",
        "call_id": "spawn1",
        "namespace": "ucode_collaboration",
        "name": "spawn_agent",
        "arguments": json.dumps(
            {"task_name": "owner", "message": "Inspect the source", "await_completion": True}
        ),
    }
    received = []

    def respond(request):
        received.append(request)
        return json_response({"object": "response", "output": [spawn]})

    with relay(respond) as url:
        first = httpx.post(
            url + "/responses",
            json={"model": "test-model", "input": [activation], "tools": tools, "stream": False},
            headers={"thread-id": "root"},
        )
        assert first.status_code == 200
        returned = first.json()["output"][0]
        assert "await_completion" not in json.loads(returned["arguments"])
        second = httpx.post(
            url + "/responses",
            json={
                "model": "test-model",
                "input": [
                    activation,
                    returned,
                    {
                        "type": "function_call_output",
                        "call_id": "spawn1",
                        "output": '{"task_name":"/root/owner"}',
                    },
                ],
                "tools": tools,
                "stream": stream,
            },
            headers={"thread-id": "root"},
        )

    assert second.status_code == 200
    assert len(received) == 1
    if stream:
        events = [
            json.loads(line[6:]) for line in second.text.splitlines() if line.startswith("data: ")
        ]
        response = events[-1]["response"]
    else:
        response = second.json()
    (wait,) = response["output"]
    assert wait["name"] == "wait_agent"
    assert wait["namespace"] == "collaboration"
    assert json.loads(wait["arguments"]) == {"timeout_ms": 3600000}
    assert "usage" not in response
