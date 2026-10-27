"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from session import Session

from ucode.agents.claude import _header_mod_files
from ucode.smart_routing import config
from ucode.smart_routing.v2 import _claude_mod_launch_options, _write_routed_claude_plugin


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
