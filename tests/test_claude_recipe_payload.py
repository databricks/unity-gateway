"""Native startup metadata and desired session state; reload is not wired yet."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.agents import LaunchOptions, claude
from ucode.constants import CLAUDE_CODE_EXTRA_BODY, SMART_ROUTER_DISABLED, SMART_ROUTER_RECIPE_LOCAL
from ucode.smart_routing.recipe_payload import claude_recipe_session, merge_claude_recipe_extra_body
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    read_session_environment,
    set_session_environment,
    start_session,
)


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


@pytest.mark.parametrize("recipe", [None, "", "custom-v4"])
def test_toggle_updates_desired_session_body_without_affecting_another_session(recipe, monkeypatch):
    if recipe is not None:
        monkeypatch.setenv("SMART_ROUTER_NAME", recipe)
    expected_recipe = recipe or "task_v3"
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    first = start_session()
    second = start_session({})
    set_session_environment({SMART_ROUTER_RECIPE_LOCAL: "other-recipe"}, path=second)
    settings = {"env": {CLAUDE_CODE_EXTRA_BODY: '{"caller_field":"keep"}'}}
    with claude_recipe_session(settings, first):
        startup_body = settings["env"][CLAUDE_CODE_EXTRA_BODY]
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
            state = read_session_environment(first)
            assert state[SMART_ROUTER_RECIPE_LOCAL] == expected
            assert json.loads(state[CLAUDE_CODE_EXTRA_BODY]) == {
                "caller_field": "keep",
                "smart_router_recipe_name": expected,
            }
            assert read_session_environment(second) == {SMART_ROUTER_RECIPE_LOCAL: "other-recipe"}
            # Desired state changes do not refresh Claude's cached startup settings.
            assert settings["env"][CLAUDE_CODE_EXTRA_BODY] == startup_body


def test_invalid_session_body_fails_toggle_without_partial_write(monkeypatch):
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    path = start_session()
    state = {SMART_ROUTER_RECIPE_LOCAL: "task_v3", CLAUDE_CODE_EXTRA_BODY: "not-json"}
    set_session_environment(state)
    result = CliRunner().invoke(cli.app, ["claude", "--disable-smart-routing"])
    assert result.exit_code == 1
    assert "CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object" in result.output
    assert read_session_environment(path) == state


@pytest.mark.parametrize(
    "flags",
    [
        {"ENABLE_SMART_ROUTING_V2": "1"},
        {"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
        {"ENABLE_SMART_ROUTING_V2": "1", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
    ],
)
@pytest.mark.parametrize("recipe", [None, "custom-v4"])
def test_pinned_headless_launch_uses_native_body_and_preserves_gateway(
    flags, recipe, monkeypatch, tmp_path
):
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    if recipe:
        monkeypatch.setenv("SMART_ROUTER_NAME", recipe)
    expected = recipe or "task_v3"
    gateway_url = "https://example.databricks.com/ai-gateway/anthropic"
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": gateway_url,
            CLAUDE_CODE_EXTRA_BODY: '{"caller_field":"keep"}',
        }
    }
    path = tmp_path / "ucode-settings.json"
    path.write_text(json.dumps(settings))
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": gateway_url}}))
    original_managed = managed.read_text()
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", path)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(claude, "_resolve_launch_binary", lambda binary: binary)
    calls = []
    monkeypatch.setattr(claude, "exec_or_spawn", calls.append)
    options = LaunchOptions(user_pinned_model="claude-sonnet-4-6", launch_smart_routing=False)
    claude.launch({}, ["-p", "hi"], options=options)
    assert len(calls) == 1
    launch_settings = json.loads(calls[0][2])
    env = launch_settings["env"]
    assert env["ANTHROPIC_BASE_URL"] == gateway_url
    assert env[SMART_ROUTER_RECIPE_LOCAL] == expected
    assert json.loads(env[CLAUDE_CODE_EXTRA_BODY]) == {
        "caller_field": "keep",
        "smart_router_recipe_name": expected,
    }
    assert (
        read_session_environment(Path(env[SESSION_ENV_VAR]))[SMART_ROUTER_RECIPE_LOCAL] == expected
    )
    assert json.loads(path.read_text()) == settings
    assert managed.read_text() == original_managed


@pytest.mark.parametrize(
    "flags", [{}, {"ENABLE_SMART_ROUTING_V2": "0", "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0"}]
)
def test_never_enabled_launch_does_not_inject_recipe(flags, monkeypatch):
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


def test_recipe_context_restores_environment_on_failure(monkeypatch):
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    monkeypatch.setenv(CLAUDE_CODE_EXTRA_BODY, '{"caller_field":"keep"}')
    path = start_session()
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match="launch failed"):
        with claude_recipe_session({}, path):
            assert os.environ[SMART_ROUTER_RECIPE_LOCAL] == "task_v3"
            raise RuntimeError("launch failed")
    assert dict(os.environ) == before
