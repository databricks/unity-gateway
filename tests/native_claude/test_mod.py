"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import os
import shutil
import subprocess
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
