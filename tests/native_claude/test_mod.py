"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from session import Session

from ucode.agents.claude import _header_mod_files
from ucode.smart_routing import config, orchestrator, session_env
from ucode.smart_routing.v2 import _claude_mod_launch_options, _write_routed_claude_plugin


@pytest.fixture(scope="module")
def native_binary():
    binary = Path(os.environ.get("UCODE_TEST_CLAUDE_BINARY", ""))
    expected = os.environ.get("UCODE_TEST_CLAUDE_VERSION", "")
    assert binary.is_absolute() and binary.is_file() and expected, (
        "Set UCODE_TEST_CLAUDE_BINARY to an absolute executable and UCODE_TEST_CLAUDE_VERSION."
    )
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected} (Claude Code)", result.stdout
    return str(binary)


def test_mod_validation_and_event_forwarding(native_binary, tmp_path):
    path = tmp_path / "plugin"
    _write_routed_claude_plugin(path, [])
    manifest = path / ".claude-plugin/plugin.json"
    doc = json.loads(manifest.read_text())
    doc["userConfig"]["routeCommand"]["default"] = [
        "/bin/ug",
        "claude-router-hook",
        "route-subagent-mod",
    ]
    manifest.write_text(json.dumps(doc))
    shutil.copytree(Path(__file__).with_name("mod_tests"), path / "tests")
    for command in ("validate", "test"):
        result = subprocess.run(
            [native_binary, "plugin", command, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "validate":
            assert "./register.ts hooks: session.start" in result.stdout


def test_header_mod_validation_and_merge(native_binary, tmp_path):
    for path, text in _header_mod_files(tmp_path).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    plugin = tmp_path / "ug-headers"
    shutil.copytree(Path(__file__).with_name("header_mod_tests"), plugin / "tests")
    for command in ("validate", "test"):
        result = subprocess.run(
            [native_binary, "plugin", command, str(plugin)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if command == "validate":
            assert "env writes: ANTHROPIC_CUSTOM_HEADERS" in result.stdout


def test_native_options_are_launch_local(native_binary, tmp_path):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ORCH_V0_CLAUDE_ONLY,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    with (
        Session(native_binary, tmp_path / "a", env) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        assert a.observe()["options"] == _claude_mod_launch_options(env)
        assert b.observe()["options"] == _claude_mod_launch_options({})
        assert not (a.plugin / "hooks/launch.json").exists()


def test_orchestrator_instructions_follow_native_toggles(native_binary, tmp_path):
    """Native request construction follows on/off/on without changing the Agent schema.

    Inference deliberately fails without credentials; this checks the serialized request,
    not a model response or delegation quality.
    """

    def agent_tool(session):
        session.send("Reply with OK.", expect_error=True)
        records = [
            json.loads(line)
            for path in session.root.glob("config/projects/*/*.jsonl")
            for line in path.read_text().splitlines()
        ]
        shapes = {
            record["shapeHash"]: record["shape"]
            for record in records
            if record.get("type") == "api-request-shape"
        }
        requests = [record for record in records if record.get("type") == "api-request"]
        assert requests, "Claude did not record a serialized API request"
        tools = shapes[requests[-1]["shapeHash"]]["tools"]
        return next(tool for tool in tools if tool["name"] == "Agent")

    env = {
        "ENABLE_SMART_ROUTING_V2": "0",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "1",
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    controls = session_env.start_session(env)
    try:
        with Session(
            native_binary,
            tmp_path / "no-orchestrator",
            {**env, "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0"},
        ) as session:
            native_tool = agent_tool(session)
        assert native_tool["description"] != orchestrator.CLAUDE_AGENT_DESCRIPTION
        routed_tool = {**native_tool, "description": orchestrator.CLAUDE_AGENT_DESCRIPTION}
        with Session(native_binary, tmp_path / "routed", env) as session:
            assert agent_tool(session) == routed_tool
            for action in ("off", "on"):
                result = session.send(f"/smart-router {action}")
                assert f"Smart Router is {action}" in result["result"]
                assert agent_tool(session) == (routed_tool if action == "on" else native_tool)
                saved = json.loads(controls.read_text())
                if action == "on":
                    assert saved == {}
                else:
                    assert saved["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "0"
        for label, overrides in (
            ("omni", {"ISAAC_LAUNCH_MODE": "omni"}),
            ("no-marker", {"UCODE_SESSION_ENV_FILE": str(tmp_path / "missing.json")}),
        ):
            with Session(native_binary, tmp_path / label, {**env, **overrides}) as session:
                assert agent_tool(session) == native_tool
    finally:
        shutil.rmtree(controls.parent)


def test_native_process_inherits_settings_and_runs_python_cli(native_binary, tmp_path):
    # The child checks launch settings before replacing itself with the real disabled router.
    script = (
        "import os, sys; "
        "assert os.environ['ENABLE_SMART_ROUTING_V2'] == '0'; "
        "assert os.environ['OAUTH_TOKEN'] == 'fixture-dummy-token'; "
        "os.execv(sys.executable, [sys.executable, '-m', 'ucode.cli', "
        "'claude-router-hook', 'route-subagent-mod', '--host', 'https://example.com'])"
    )
    with Session(
        native_binary,
        tmp_path / "process with spaces",
        {
            "ENABLE_SMART_ROUTING_V2": "0",
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
            "OAUTH_TOKEN": "fixture-dummy-token",
        },
        route_command=[sys.executable, "-c", script],
    ) as session:
        result = session.observe()["route"]
        assert result["exitCode"] == 0, result
        assert result["stdout"] == result["stderr"] == "", result
