"""File-only values and isolated process-environment composition."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.test_managed_source import wire
from ucode import child_env, managed_ownership
from ucode.managed_source import validate_file_config


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_exact_values_survive_file_parser_and_child_composition(agent):
    values = {"EMPTY": "", "SPACE": "  ", "MULTILINE": "one\ntwo\n", "UNICODE": "雪"}
    raw = wire(agent)
    raw["enabled_agents"][0]["config"]["custom_env"] = values
    parsed = validate_file_config(raw, agent)
    assert parsed["enabled_agents"][agent]["custom_env"] == values
    parent = dict(os.environ)
    child = child_env.build_child_env(
        {f"{agent}_custom_env": values}, agent, inherited={"KEEP": "yes"}
    )
    assert child == {"KEEP": "yes", **values}
    assert dict(os.environ) == parent


@pytest.mark.parametrize(
    "values",
    [
        None,
        [],
        {"SECRET": 42},
        {"SECRET": False},
        {"SECRET": None},
        {"SECRET": "hidden\x00value"},
        {"SECRET": "hidden\ud800value"},
        {"BAD-NAME": "hidden"},
        {"1BAD": "hidden"},
        {"": "hidden"},
        {"雪": "hidden"},
    ],
)
def test_invalid_values_are_rejected_without_echoing_contents(values):
    with pytest.raises(RuntimeError) as error:
        child_env.validate_custom_env(values, "custom_env", "codex", {})
    assert "hidden" not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "OPENAI_BASE_URL",
        "HOME",
        "USERPROFILE",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "DATABRICKS_CONFIG_FILE",
        "DATABRICKS_BEARER_COMMAND",
        "DATABRICKS_CONFIG_PROFILE",
        "UG_WORKSPACE",
        "SMART_ROUTER_NAME",
        "UCODE_CLAUDE_V2_SOCKET",
        "ENABLE_SMART_ROUTING_V2",
        "ENABLE_CUSTOM_OAUTH_FROM_CLI",
        "home",
        "PATHEXT",
        child_env.ORIGINAL_PATH_ENV,
    ],
)
def test_reserved_controls_fail_without_values(name):
    with pytest.raises(RuntimeError, match="reserved environment variable") as error:
        child_env.validate_custom_env({name: "hidden-value"}, "custom_env", "claude", {})
    assert name in str(error.value)
    assert "hidden-value" not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
        "OTEL_TRACES_EXPORTER",
        "OTEL_SDK_DISABLED",
        "CLAUDE_CODE_ENABLE_TELEMETRY",
    ],
)
def test_tracing_conflicts_check_raw_and_resolved_state(name):
    values = {name: "hidden-exporter"}
    assert child_env.validate_custom_env(values, "custom_env", "codex", {}) == values
    with pytest.raises(RuntimeError, match="conflict with enabled UG tracing") as error:
        child_env.validate_custom_env(values, "custom_env", "codex", {"tracing": {"enabled": True}})
    assert "hidden-exporter" not in str(error.value)
    with pytest.raises(RuntimeError, match="conflict with enabled UG tracing"):
        child_env.build_child_env({"codex_custom_env": values, "codex_otel_tracing": True}, "codex")


def test_resource_and_nontrace_signal_controls_remain_available():
    values = {
        "OTEL_SERVICE_NAME": "worker",
        "OTEL_RESOURCE_ATTRIBUTES": "key=value",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "https://metrics.example",
        "OTEL_LOGS_EXPORTER": "none",
    }
    assert (
        child_env.build_child_env(
            {"claude_custom_env": values, "claude_otel_tracing": True}, "claude", inherited={}
        )
        == values
    )


def test_retired_names_scrub_inheritance_and_generated_values_win(monkeypatch):
    monkeypatch.setattr(managed_ownership, "retired_environment", lambda _agent: {"OLD"})
    inherited = {"OLD": "stale", "KEEP": "original", "OAUTH_TOKEN": "expired", "DISCOVERY": "stale"}
    state = {
        "codex_custom_env": {"KEEP": "new", "EMPTY": ""},
        "claude_custom_env": {"OTHER": "claude"},
    }
    first = child_env.build_child_env(
        state, "codex", inherited=inherited, generated={"OAUTH_TOKEN": "fresh", "DISCOVERY": None}
    )
    second = child_env.build_child_env(state, "claude", inherited=inherited)
    assert first == {"KEEP": "new", "EMPTY": "", "OAUTH_TOKEN": "fresh"}
    assert second == {
        "KEEP": "original",
        "OTHER": "claude",
        "OAUTH_TOKEN": "expired",
        "DISCOVERY": "stale",
    }
    assert inherited["OLD"] == "stale"
    assert "EMPTY" not in second


def test_windows_environment_identity_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(child_env, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(managed_ownership, "retired_environment", lambda _agent: {"Old"})
    with pytest.raises(RuntimeError, match="duplicate environment name"):
        child_env.agent_custom_env({"codex_custom_env": {"Test": "a", "TEST": "b"}}, "codex")
    child = child_env.build_child_env(
        {"codex_custom_env": {"Path": "child"}},
        "codex",
        inherited={"OLD": "stale", "PATH": "parent", "oauth_token": "expired"},
        generated={"OAUTH_TOKEN": "fresh"},
    )
    assert child == {"Path": "child", "OAUTH_TOKEN": "fresh", child_env.ORIGINAL_PATH_ENV: "parent"}


def test_initial_binary_is_resolved_against_parent_path(tmp_path, monkeypatch):
    parent_bin = tmp_path / "parent"
    child_bin = tmp_path / "child"
    for directory in (parent_bin, child_bin):
        directory.mkdir()
        executable = directory / "coding-agent"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(parent_bin))
    argv = child_env.resolve_agent_argv(["coding-agent", "--help"])
    environment = child_env.build_child_env({"codex_custom_env": {"PATH": str(child_bin)}}, "codex")
    assert argv == [str(parent_bin / "coding-agent"), "--help"]
    assert environment["PATH"] == str(child_bin)
    assert os.environ["PATH"] == str(parent_bin)
    with pytest.raises(RuntimeError, match="Cannot find the agent executable"):
        child_env.resolve_agent_argv(["does-not-exist"])


def test_real_safe_child_and_ordinary_descendant_inherit_exact_values(tmp_path):
    values = {"UG_TEST_EMPTY": "", "UG_TEST_MULTILINE": " first\nsecond \n"}
    environment = child_env.build_child_env(
        {"codex_custom_env": values}, "codex", inherited={"HOME": str(tmp_path)}
    )
    descendant = "import json, os; print(json.dumps({k: os.environ[k] for k in ('UG_TEST_EMPTY', 'UG_TEST_MULTILINE')}))"
    script = "import subprocess, sys; subprocess.run([sys.executable, '-c', sys.argv[1]], check=True, timeout=10)"
    result = subprocess.run(
        [sys.executable, "-c", script, descendant],
        env=environment,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == values
    assert "UG_TEST_MULTILINE" not in os.environ


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_v2_conflict_rejected_before_auth_or_process_preparation(agent, monkeypatch, tmp_path):
    from ucode.smart_routing import v2

    monkeypatch.setattr(
        v2, "_launch_token", lambda *_args: pytest.fail("must validate before authentication")
    )
    monkeypatch.setattr(
        v2,
        "resolve_agent_argv",
        lambda *_args: pytest.fail("must validate before process preparation"),
    )
    state = {
        "workspace": "https://example.com",
        f"{agent}_otel_tracing": True,
        f"{agent}_custom_env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "hidden-conflict"},
    }
    with pytest.raises(RuntimeError, match="conflict with enabled UG tracing") as error:
        if agent == "codex":
            v2.launch_codex(
                state,
                [],
                binary="codex",
                start_model="gpt-test",
                render_overlay=lambda *_args, **_kwargs: {},
            )
        else:
            v2.launch_claude(
                state,
                [],
                binary="claude",
                user_settings_path=tmp_path / "user.json",
                launch_model=None,
                compose_settings=lambda _args: ({}, []),
                launch_model_args=lambda *_args: [],
                model_name=lambda model: model,
            )
    assert "hidden-conflict" not in str(error.value)


def test_original_auth_path_survives_child_rebuild_and_retirement(monkeypatch):
    inherited = {"PATH": "/original/tools", "KEEP": "user"}
    state = {"codex_custom_env": {"PATH": "/child/tools"}}
    child = child_env.build_child_env(state, "codex", inherited=inherited)
    rebuilt = child_env.build_child_env(state, "codex", inherited=child)
    assert child == rebuilt
    assert rebuilt["PATH"] == "/child/tools"
    assert rebuilt[child_env.ORIGINAL_PATH_ENV] == "/original/tools"
    assert child_env.auth_dependency_env(rebuilt)["PATH"] == "/original/tools"
    assert child_env.auth_dependency_env(inherited) is None
    monkeypatch.setattr(managed_ownership, "retired_environment", lambda _agent: {"PATH"})
    omitted = child_env.build_child_env({}, "codex", inherited=inherited)
    assert "PATH" not in omitted
    assert child_env.auth_dependency_env(omitted)["PATH"] == "/original/tools"
    assert inherited["PATH"] == "/original/tools"
