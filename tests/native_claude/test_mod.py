"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from session import Session

from ucode.smart_routing import config, savings
from ucode.smart_routing.v2 import (
    CLAUDE_SAVINGS_OPTION_DEFAULTS,
    _claude_mod_launch_options,
    _write_routed_claude_plugin,
)


def _savings_options(root: Path) -> dict:
    return {
        # A space and a non-ASCII character check the argv survives the options round trip.
        "savings_pricer": savings.pricer_argv(
            python=sys.executable,
            price_cache=root / "no prices λ.json",
            baseline_session_start=False,
        ),
        "savings_usage": str(root / savings.USAGE_FILENAME),
        "orchestrator_version": "1.2.0",
    }


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
            hooks = next(
                line for line in result.stdout.splitlines() if "./register.ts hooks:" in line
            )
            assert "session.start" in hooks


@pytest.mark.parametrize(
    "version", [config.SUBAGENT_ORCH_V0_CLAUDE_ONLY, config.SUBAGENT_ORCH_SAVINGS_V0]
)
def test_native_options_are_launch_local(native_binary, tmp_path, version):
    env = {
        "SMART_ROUTER_CONFIG_VERSION": version,
        "SMART_ROUTER_NAME": 'custom "quoted" λ',
    }
    savings_options = _savings_options(tmp_path / "a")
    with (
        Session(native_binary, tmp_path / "a", env, options=savings_options) as a,
        Session(native_binary, tmp_path / "b", {}) as b,
    ):
        assert a.observe()["options"] == {**_claude_mod_launch_options(env), **savings_options}
        # Claude fills in the declared defaults for options a launch omits.
        assert b.observe()["options"] == {
            **_claude_mod_launch_options({}),
            **CLAUDE_SAVINGS_OPTION_DEFAULTS,
        }
        assert not (a.plugin / "hooks/launch.json").exists()


def test_savings_mod_records_the_start_model(native_binary, tmp_path):
    root = tmp_path / "session"
    options = _savings_options(root)
    usage = Path(options["savings_usage"])
    with Session(
        native_binary, root, {"ENABLE_SMART_ROUTING_SAVINGS": "1"}, options=options
    ) as session:
        session.observe()
        # The mod writes its sums off the session.start critical path.
        deadline = time.monotonic() + 10
        while True:
            try:
                recorded = json.loads(usage.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                assert time.monotonic() < deadline, "the savings mod never wrote its usage file"
                time.sleep(0.1)

    assert recorded == {
        "version": 1,
        "start_model": "claude-sonnet-4-6",
        "main_model": None,
        "first_main_model": None,
        "user_switched": False,
        "entries": [],
    }


@pytest.mark.parametrize("flag", [None, "", "0"])
def test_disabled_savings_does_not_create_usage_file(native_binary, tmp_path, flag):
    root = tmp_path / "session"
    options = _savings_options(root)
    env = {} if flag is None else {"ENABLE_SMART_ROUTING_SAVINGS": flag}
    # Stale savings options must not enable the feature without an explicit flag.
    with Session(native_binary, root, env, options=options) as session:
        session.observe()
    assert not Path(options["savings_usage"]).exists()


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
