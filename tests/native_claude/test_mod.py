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

from ucode.smart_routing import config
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


def test_status_reads_session_overrides_without_inference(native_binary, tmp_path):
    session_file = tmp_path / "env.json"
    env = {
        "SMART_ROUTER_CONFIG_VERSION": config.SUBAGENT_ONLY_V0,
        "UCODE_SESSION_ENV_FILE": str(session_file),
        "UCODE_SMART_ROUTER_PYTHON": sys.executable,
    }
    with Session(native_binary, tmp_path / "status", env) as session:
        for overrides, expected in [
            ({}, "on"),
            ({"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0"}, "off"),
            ({}, "on"),
        ]:
            session_file.write_text(json.dumps(overrides))
            before = session_file.read_bytes()
            result = session.send("/smart-router status")
            assert f"Smart Router is {expected} for this session." in result["result"]
            assert result["num_turns"] == 0 and result["usage"]["output_tokens"] == 0
            assert session_file.read_bytes() == before
        session_file.write_text("invalid json")
        result = session.send("/smart-router status")
        assert "Could not read Smart Router status" in result["result"]
        assert session_file.read_text() == "invalid json"
