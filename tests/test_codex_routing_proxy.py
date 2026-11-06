from __future__ import annotations

import copy
import json
import socket
from urllib.parse import urlsplit

import pytest

from ucode.config_io import atomic_write_json
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.gateway_proxy import RequestRewriteError
from ucode.smart_routing import codex_proxy
from ucode.smart_routing.codex_proxy import RoutingAttribution


@pytest.mark.parametrize(
    "model", ["system.ai.kimi-k3", "system.ai.glm-5-2", "system.ai.deepseek-v4"]
)
@pytest.mark.parametrize("disabled", [False, True])
def test_oss_receives_assignments_followups_and_child_results(tmp_path, model, disabled):
    controls = tmp_path / "env.json"
    atomic_write_json(controls, dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0") if disabled else {})
    messages = [
        {"type": "message", "role": "user", "content": "original parent task"},
        {
            "type": "agent_message",
            "recipient": "/root/worker",
            "content": [
                {
                    "type": "input_text",
                    "text": "Message Type: NEW_TASK\nSender: /root\nPayload:\nRead café λ.txt",
                }
            ],
        },
        {"type": "function_call_output", "call_id": "read", "output": "file value"},
        {
            "type": "agent_message",
            "recipient": "/root/worker",
            "content": [
                {
                    "type": "input_text",
                    "text": "Message Type: NEW_TASK\nSender: /root\nPayload:\nFollow-up task",
                }
            ],
        },
        {
            "type": "agent_message",
            "recipient": "/root",
            "content": [
                {
                    "type": "input_text",
                    "text": "Message Type: FINAL_ANSWER\nSender: /root/worker\nPayload:\nDone",
                }
            ],
        },
    ]
    body, headers = RoutingAttribution(controls, "stable")(
        "/responses", json.dumps({"model": model, "input": messages}).encode()
    )
    translated = json.loads(body)["input"]
    assert translated[0] == messages[0]
    assert translated[2] == messages[2]
    for index in (1, 3, 4):
        assert translated[index] == {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"Recipient: {messages[index]['recipient']}\n"},
                *messages[index]["content"],
            ],
        }
    assert headers["Databricks-Smart-Router-Recipe"] == ("DISABLED" if disabled else "stable")


@pytest.mark.parametrize("model", ["gpt-6-sol", "system.ai.gpt-6-luna", "custom.provider"])
def test_other_models_keep_native_agent_messages(tmp_path, model):
    controls = tmp_path / "env.json"
    atomic_write_json(controls, {})
    item = {"type": "agent_message", "recipient": "/root", "encrypted_content": "opaque"}
    body, _ = RoutingAttribution(controls, "stable")(
        "/responses", json.dumps({"model": model, "input": [item]}).encode()
    )
    assert json.loads(body)["input"] == [item]


def test_encrypted_only_oss_assignment_fails_instead_of_disappearing(tmp_path):
    controls = tmp_path / "env.json"
    atomic_write_json(controls, {})
    with pytest.raises(RequestRewriteError, match="plaintext Codex agent messages"):
        RoutingAttribution(controls, "stable")(
            "/responses",
            json.dumps(
                {
                    "model": "system.ai.kimi-k3",
                    "input": [{"type": "agent_message", "encrypted_content": "opaque"}],
                }
            ).encode(),
        )


@pytest.mark.parametrize("metadata_shape", ["missing", "string", "object"])
@pytest.mark.parametrize(
    "path", ["/ai-gateway/mlflow/v1/responses", "/ai-gateway/mlflow/v1/responses/compact?x=1"]
)
def test_recipe_tracks_controls_and_preserves_other_request_fields(tmp_path, metadata_shape, path):
    controls = tmp_path / "env.json"
    payload = {
        "model": "gpt-6-luna",
        "input": [{"type": "message", "content": "task"}],
        "stream": True,
    }
    if metadata_shape != "missing":
        metadata = {"smart_router_recipe_name": "stale", "turn_id": "preserved"}
        payload["client_metadata"] = {
            "custom": "preserved",
            "x-codex-turn-metadata": json.dumps(metadata)
            if metadata_shape == "string"
            else metadata,
        }
    original = copy.deepcopy(payload)
    rewriter = RoutingAttribution(controls, "launch-recipe")
    for values, recipe in [
        ({}, "launch-recipe"),
        (dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), "DISABLED"),
        ({}, "launch-recipe"),
    ]:
        atomic_write_json(controls, values)
        body, headers = rewriter(path, json.dumps(payload).encode())
        result = json.loads(body)
        metadata = result.pop("client_metadata")
        turn = metadata.pop("x-codex-turn-metadata")
        assert isinstance(turn, str) == (metadata_shape != "object")
        turn = json.loads(turn) if isinstance(turn, str) else turn
        assert turn.pop("smart_router_recipe_name") == recipe
        assert headers == {"Databricks-Smart-Router-Recipe": recipe}
        if metadata_shape != "missing":
            assert turn == {"turn_id": "preserved"}
            assert metadata == {"custom": "preserved"}
        assert result == {k: v for k, v in payload.items() if k != "client_metadata"}
    assert payload == original


@pytest.mark.parametrize("contents", [None, "{", "[]", '{"unexpected":"0"}'])
def test_unreadable_controls_reject_inference_instead_of_using_stale_recipe(tmp_path, contents):
    controls = tmp_path / "env.json"
    if contents is not None:
        controls.write_text(contents)
    with pytest.raises(RequestRewriteError, match="session controls") as exc:
        RoutingAttribution(controls, "stable")("/responses", b'{"model":"m"}')
    assert exc.value.status == 503


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b'{"client_metadata":null}',
        b'{"client_metadata":{"x-codex-turn-metadata":"bad"}}',
    ],
)
def test_invalid_metadata_rejects_only_that_request(tmp_path, body):
    controls = tmp_path / "env.json"
    atomic_write_json(controls, {})
    rewrite = RoutingAttribution(controls, "stable")
    with pytest.raises(RequestRewriteError) as exc:
        rewrite("/responses", body)
    assert exc.value.status == 400
    result, headers = rewrite("/responses", b'{"model":"m"}')
    assert json.loads(result)["model"] == "m"
    assert headers["Databricks-Smart-Router-Recipe"] == "stable"


@pytest.mark.parametrize("fail_launch", [False, True])
def test_proxy_lifetime_does_not_write_provider_or_leave_listener(
    tmp_path, monkeypatch, fail_launch
):
    token_calls = []

    def token(state, workspace, *, force_refresh):
        token_calls.append((state["profile"], workspace, force_refresh))
        return "test-credential"

    monkeypatch.setattr(codex_proxy, "launch_token", token)
    controls = tmp_path / "env.json"
    atomic_write_json(controls, {})
    overlay = {
        "model_provider": "Databricks",
        "model_providers": {
            "Databricks": {
                "base_url": "https://example.databricks.com/ai-gateway/codex/v1",
                "auth": {"command": "original-helper"},
                "http_headers": {"Databricks-Smart-Router-Recipe": "stable", "Custom": "kept"},
            }
        },
    }
    original = copy.deepcopy(overlay)
    state = {"workspace": "https://example.databricks.com", "profile": "selected-profile"}
    failed = False
    try:
        with codex_proxy.session_proxy(state, overlay, controls) as (config, env):
            provider = config["model_providers"][config["model_provider"]]
            endpoint = urlsplit(provider["base_url"])
            assert provider["env_key"] == codex_proxy.PROXY_KEY_ENV
            assert env[codex_proxy.PROXY_KEY_ENV]
            assert "auth" not in provider
            assert provider["http_headers"]["Custom"] == "kept"
            assert endpoint.path == "/ai-gateway/codex/v1"
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=2):
                pass
            if fail_launch:
                raise OSError("agent did not start")
    except OSError:
        if not fail_launch:
            raise
        failed = True
    assert failed == fail_launch
    assert token_calls == [("selected-profile", state["workspace"], True)]
    assert overlay == original
    with pytest.raises(OSError):
        socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
