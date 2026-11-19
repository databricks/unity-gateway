"""Startup validation with controlled HTTP and real background workers."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from email.message import Message
from unittest.mock import Mock

import pytest

from ucode import databricks
from ucode.smart_routing import startup_validation as validation
from ucode.smart_routing.session_env import session_routing_enabled
from ucode.workspace import activate


def _wait_for_result(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail("validation worker did not publish a result")


def test_delayed_http_is_nonblocking_and_preserves_resolved_menu(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    captured = {}

    def post(url, token, body, **kwargs):
        captured.update(url=url, token=token, body=body)
        entered.set()
        assert release.wait(3)
        return {"is_supported": True}, None

    monkeypatch.setattr(validation, "_http_post_json", post)
    controls = tmp_path / "env.json"
    options = [("custom.schema.service.model", "codex"), ("system.ai.gpt-5", "codex")]
    try:
        validation.start(
            "https://example.com",
            "test-token",
            options,
            router_name="chosen-recipe",
            session_controls=controls,
        )
        assert entered.wait(1)
        assert not release.is_set()
        assert not validation.result_path(controls).exists()
        options.clear()
    finally:
        release.set()
    assert _wait_for_result(validation.result_path(controls)) == {"message": None}
    assert captured == {
        "url": "https://example.com/ai-gateway/routing/v1/routes:validate",
        "token": "test-token",
        "body": {
            "route_options": [
                {"model": "custom.schema.service.model", "harness": "codex"},
                {"model": "system.ai.gpt-5", "harness": "codex"},
            ],
            "route_selector": {"router_name": "chosen-recipe"},
        },
    }


@pytest.mark.parametrize(
    "response",
    [
        ({"is_supported": True}, None),
        (
            {
                "is_supported": False,
                "metadata": {"routing_unsupported_reason": "Need two families."},
                "ineligible_route_options": [
                    {"model": "custom.model", "reason": "No matching harness."}
                ],
                "available_route_options": [{"model": "system.ai.other", "harness": "claude"}],
            },
            None,
        ),
        (None, "HTTP 503: secret server diagnostics"),
        ({"is_supported": "false"}, None),
        RuntimeError("private token diagnostics"),
    ],
)
def test_unsupported_menu_turns_routing_off_and_failures_keep_it_on(
    response, tmp_path, monkeypatch
):
    post = (
        Mock(side_effect=response)
        if isinstance(response, Exception)
        else Mock(return_value=response)
    )
    monkeypatch.setattr(validation, "_http_post_json", post)
    controls = tmp_path / "env.json"
    controls.write_text("{}")
    validation.start(
        "https://example.com",
        "test-token",
        [("custom.model", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    result = _wait_for_result(validation.result_path(controls))
    message = result["message"]
    post.assert_called_once()
    unsupported = isinstance(response, tuple) and (response[0] or {}).get("is_supported") is False
    assert session_routing_enabled(controls) is not unsupported
    assert result.get("routing_disabled", False) is unsupported
    if isinstance(response, tuple) and response[0] and response[0].get("is_supported") is True:
        assert message is None
    elif unsupported:
        assert "Need two families." in message
        assert "custom.model: No matching harness." in message
        assert "system.ai.other (claude)" in message
        assert message.endswith("Smart routing is off for this session.")
    else:
        assert message == validation._UNAVAILABLE
        assert controls.read_text() == "{}"


def test_request_uses_existing_http_auth_and_workspace_selection(tmp_path, monkeypatch):
    captured = []
    activate("https://example.com", "111")

    class Response(io.BytesIO):
        status = 200
        headers = Message()

    def urlopen(request, **kwargs):
        captured.append(request)
        return Response(b'{"is_supported":true}')

    monkeypatch.setattr(validation, "_http_post_json", databricks._http_post_json)
    monkeypatch.setattr(databricks.urllib_request, "urlopen", urlopen)
    controls = tmp_path / "env.json"
    validation.start(
        "https://example.com?w=111",
        "test-token",
        [("original.model", "claude")],
        router_name="stable",
        session_controls=controls,
    )
    assert _wait_for_result(validation.result_path(controls)) == {"message": None}
    assert len(captured) == 1
    request = captured[0]
    assert request.full_url == "https://example.com/ai-gateway/routing/v1/routes:validate"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("X-databricks-workspace-id") == "111"
    assert request.get_header("X-databricks-org-id") == "111"


def test_worker_start_failure_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validation.threading.Thread, "start", Mock(side_effect=RuntimeError("threads unavailable"))
    )
    validation.start(
        "https://example.com",
        "test-token",
        [],
        router_name="stable",
        session_controls=tmp_path / "env.json",
    )


def test_result_write_failure_is_isolated(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        validation.threading, "Thread", lambda *, target, daemon: SimpleNamespace(start=target)
    )
    writer = Mock(side_effect=RuntimeError("disk unavailable"))
    monkeypatch.setattr(validation, "atomic_write_json", writer)
    validation.start(
        "https://example.com",
        "test-token",
        [],
        router_name="stable",
        session_controls=tmp_path / "env.json",
    )
    writer.assert_called_once()


@pytest.mark.parametrize("message", [None, "Smart Router compatibility check unavailable."])
def test_codex_notification_is_async_once_and_keeps_connection_open(message, tmp_path):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    from ucode.smart_routing.codex_interposer import _serve

    async def exercise():
        async def echo(socket):
            async for frame in socket:
                await socket.send(frame)

        controls = tmp_path / "env.json"
        async with serve(echo, "127.0.0.1", 0) as upstream:
            port = upstream.sockets[0].getsockname()[1]
            interposer = await _serve(
                "127.0.0.1",
                0,
                f"ws://127.0.0.1:{port}",
                None,
                lambda _: None,
                session_controls=controls,
            )
            try:
                proxy_port = interposer.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{proxy_port}") as tui:
                    await tui.send('{"id":1,"result":{}}')
                    assert await asyncio.wait_for(tui.recv(), 1) == '{"id":1,"result":{}}'
                    validation.result_path(controls).write_text(json.dumps({"message": message}))
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(tui.recv(), 0.15)
                    await tui.send('{"method":"initialized"}')
                    received = [await asyncio.wait_for(tui.recv(), 1)]
                    if message:
                        received.append(await asyncio.wait_for(tui.recv(), 1))
                        assert {
                            "method": "warning",
                            "params": {"message": message},
                        } in [json.loads(frame) for frame in received]
                    assert {"method": "initialized"} in [json.loads(frame) for frame in received]
                    await asyncio.sleep(0.2)
                    await tui.send('{"id":2,"result":{}}')
                    assert await asyncio.wait_for(tui.recv(), 1) == '{"id":2,"result":{}}'
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(tui.recv(), 0.15)
            finally:
                interposer.close()
                await interposer.wait_closed()

    asyncio.run(exercise())
