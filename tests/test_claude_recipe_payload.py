"""Claude session controls and per-request recipe propagation."""

import gzip
import io
import json
import os
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from ucode import cli, gateway_proxy
from ucode.agents import LaunchOptions, claude
from ucode.constants import SMART_ROUTER_DISABLED, SMART_ROUTER_RECIPE_LOCAL
from ucode.smart_routing.recipe_payload import (
    CLAUDE_CODE_EXTRA_BODY,
    claude_recipe_session,
    merge_claude_recipe_extra_body,
    session_recipe,
)
from ucode.smart_routing.session_env import SESSION_ENV_VAR, start_session


def test_body_merge_preserves_existing_fields_and_escapes_recipe():
    merged = merge_claude_recipe_extra_body('{"caller_field":{"keep":true}}', 'custom<&"recipe')
    assert json.loads(merged) == {
        "caller_field": {"keep": True},
        "smart_router_recipe_name": 'custom<&"recipe',
    }


@pytest.mark.parametrize("raw", ["not-json", "[]", "null", "42"])
def test_malformed_body_is_an_actionable_launch_error(raw):
    with pytest.raises(
        RuntimeError, match="CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object"
    ):
        merge_claude_recipe_extra_body(raw, "task_v3")


def test_managed_endpoint_conflict_blocks_launch_before_starting_a_forwarder(tmp_path, monkeypatch):
    path = tmp_path / "managed-settings.json"
    original = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://managed.example"}})
    path.write_text(original)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: path)
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    session = Mock()
    monkeypatch.setattr(claude, "claude_recipe_session", session)
    with pytest.raises(RuntimeError, match="OS-managed ANTHROPIC_BASE_URL"):
        claude.launch({}, [], options=LaunchOptions())
    session.assert_not_called()
    assert path.read_text() == original


@pytest.mark.parametrize("recipe", [None, "custom-v4"])
def test_real_session_requests_follow_on_off_on_without_affecting_another_session(
    recipe, monkeypatch
):
    if recipe:
        monkeypatch.setenv("SMART_ROUTER_NAME", recipe)
    expected_recipe = recipe or "task_v3"
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    first = start_session()
    captured = []

    def upstream(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, stream=httpx.ByteStream(b"data: streaming-response\n\n"))

    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://example.com/ai-gateway/anthropic",
            CLAUDE_CODE_EXTRA_BODY: '{"caller_field":"keep"}',
        }
    }
    start_proxy = gateway_proxy.start_claude_recipe_proxy

    def fixture_proxy(upstream_url, provider):
        server, client = start_proxy(upstream_url, provider)
        client.close()
        fixture_client = httpx.Client(
            base_url=upstream_url.rstrip("/") + "/",
            transport=httpx.MockTransport(upstream),
        )
        server.RequestHandlerClass.client = fixture_client
        return server, fixture_client

    monkeypatch.setattr(gateway_proxy, "start_claude_recipe_proxy", fixture_proxy)
    with claude_recipe_session(settings, first):
        assert json.loads(settings["env"][CLAUDE_CODE_EXTRA_BODY]) == {
            "caller_field": "keep",
            "smart_router_recipe_name": expected_recipe,
        }
        second_env = {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"}
        second = start_session(second_env)
        second.write_text(json.dumps({SMART_ROUTER_RECIPE_LOCAL: "other-recipe"}))
        for action, expected in [
            (None, expected_recipe),
            ("off", SMART_ROUTER_DISABLED),
            ("on", expected_recipe),
        ]:
            if action:
                result = CliRunner().invoke(
                    cli.app,
                    ["claude", f"--{'disable' if action == 'off' else 'enable'}-smart-routing"],
                )
                assert result.exit_code == 0, result.output
            assert session_recipe(first) == expected
            assert session_recipe(second) == "other-recipe"
            for source in ("parent", "child"):
                # Native startup settings can keep sending the initial recipe; the boundary
                # must replace it with the current session value on every inference.
                response = httpx.post(
                    settings["env"]["ANTHROPIC_BASE_URL"] + "/v1/messages",
                    json={
                        "model": "claude-sonnet-4-6",
                        "smart_router_recipe_name": expected_recipe,
                        "caller_field": "keep",
                        "metadata": {"source": source},
                    },
                    headers={"Authorization": "Bearer native-auth"},
                    trust_env=False,
                )
                assert response.status_code == 200
                assert response.content == b"data: streaming-response\n\n"
                assert captured[-1]["smart_router_recipe_name"] == expected
                assert captured[-1]["caller_field"] == "keep"
                assert captured[-1]["metadata"] == {"source": source}


@pytest.mark.parametrize("compressed", [False, True])
def test_body_refresh_reads_session_state_for_each_request(tmp_path, monkeypatch, compressed):
    state = tmp_path / "env.json"
    handler = object.__new__(gateway_proxy._ClaudeRecipeProxyHandler)
    handler.command = "POST"
    handler.path = "/v1/messages?beta=true"
    handler.headers = {"Content-Encoding": "gzip"} if compressed else {}
    handler.recipe_provider = lambda: session_recipe(state)
    original = json.dumps({"smart_router_recipe_name": "stale", "caller_field": "keep"}).encode()
    body = gzip.compress(original) if compressed else original
    for recipe in ("task_v3", SMART_ROUTER_DISABLED, "custom-v4"):
        state.write_text(json.dumps({SMART_ROUTER_RECIPE_LOCAL: recipe}))
        updated = handler._request_body(body)
        assert updated is not None
        assert json.loads(gzip.decompress(updated) if compressed else updated) == {
            "smart_router_recipe_name": recipe,
            "caller_field": "keep",
        }


def test_proxy_preserves_native_auth_and_non_inference_body():
    handler = object.__new__(gateway_proxy._ClaudeRecipeProxyHandler)
    handler.command = "POST"
    handler.path = "/v1/messages/count_tokens"
    handler.headers = {
        "Authorization": "Bearer native-auth",
        "X-Databricks-AI-Gateway-Token": "Bearer relay-auth",
        "Databricks-Model-Provider-Service": "provider",
        "Content-Length": "42",
        "Host": "127.0.0.1",
    }
    assert handler._request_body(b"original") == b"original"
    assert handler._request_headers("", frozenset()) == {
        "Authorization": "Bearer native-auth",
        "X-Databricks-AI-Gateway-Token": "Bearer relay-auth",
        "Databricks-Model-Provider-Service": "provider",
    }


def test_corrupt_session_state_blocks_inference_instead_of_sending_stale_recipe(tmp_path):
    state = tmp_path / "env.json"
    state.write_text("not-json")
    handler = object.__new__(gateway_proxy._ClaudeRecipeProxyHandler)
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.recipe_provider = lambda: session_recipe(state)
    handler.client = Mock()
    handler._safe_send_error = Mock()
    handler._handle()
    handler._safe_send_error.assert_called_once_with(
        503, "Smart Router session recipe could not be read"
    )
    handler.client.stream.assert_not_called()


@pytest.mark.parametrize("flag", ["ENABLE_SMART_ROUTING_V2", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY"])
def test_pinned_headless_launch_still_initializes_recipe(flag, monkeypatch):
    monkeypatch.setenv(flag, "1")
    monkeypatch.setattr(claude, "_compose_v2_settings", lambda args: ({}, args))
    monkeypatch.setattr(claude.smart_routing_v2, "_prepare_smart_router_session", Mock())
    session = Mock()
    session.return_value.__enter__ = Mock(
        return_value={"env": {SMART_ROUTER_RECIPE_LOCAL: "task_v3"}}
    )
    session.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(claude, "claude_recipe_session", session)
    launch = Mock()
    monkeypatch.setattr(claude, "_launch", launch)
    options = LaunchOptions(user_pinned_model="claude-sonnet-4-6", launch_smart_routing=False)
    claude.launch({}, ["-p", "hi"], options=options)
    session.assert_called_once()
    assert launch.call_args.kwargs["recipe_settings"]["env"][SMART_ROUTER_RECIPE_LOCAL] == "task_v3"


@pytest.mark.parametrize(
    "flags", [{}, {"ENABLE_SMART_ROUTING_V2": "0", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0"}]
)
def test_never_enabled_launch_does_not_install_recipe_or_forwarder(flags, monkeypatch):
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    session = Mock(side_effect=AssertionError("never-enabled sessions must not inject a recipe"))
    monkeypatch.setattr(claude, "claude_recipe_session", session)
    launch = Mock()
    monkeypatch.setattr(claude, "_launch", launch)
    claude.launch({}, [], options=LaunchOptions(launch_smart_routing=False))
    assert launch.call_args.kwargs["recipe_settings"] is None
    assert SMART_ROUTER_RECIPE_LOCAL not in os.environ
    assert SESSION_ENV_VAR not in os.environ
