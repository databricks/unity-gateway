"""Validate and exercise the mod entry point using an explicitly selected Claude."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from session import Session

from tests.integration.utils.terminal import AgentTerminal
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


def test_native_tui_routing_cards_are_clean_and_wrapped(native_binary, tmp_path):
    plugin = tmp_path / "plugin"
    _write_routed_claude_plugin(plugin, [])
    shutil.copyfile(
        Path(__file__).with_name("routing_preview.ts"), plugin / "hooks/routing-preview.ts"
    )
    (plugin / "hooks/hooks.json").write_text('{"modules":["./routing-preview.ts"]}')
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
        "ANTHROPIC_API_KEY": "fixture-dummy-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    command = [
        native_binary,
        "--setting-sources",
        "user",
        "--plugin-dir",
        str(plugin),
        "--model",
        "claude-sonnet-4-6",
    ]
    session = SimpleNamespace(cwd=tmp_path, home=tmp_path, env=env)
    terminal = AgentTerminal(session, "claude", command, "routing-card-preview")
    try:
        terminal.boot(timeout=45)
        if "Do you want to use this API key?" in terminal.visible:
            terminal.choose("Do you want to use this API key?", "Yes")
            terminal.boot(timeout=45)
        terminal.submit("/routing-card-preview")
        terminal.wait_for(
            lambda screen: (
                "Compute 1+1 (agent A)" in screen
                and "Compute 1+1 (agent B)" in screen
                and "Preview only; no model request." in screen
            ),
            "both routing cards",
            timeout=20,
        )
        cards = terminal.visible[terminal.visible.index("Compute 1+1 (agent B)") :]
        assert cards.count("↳ Selected Model: claude-haiku-5-5") == 2, cards
        assert " · 2.8s" in cards and " · 3.6s" in cards, cards
        assert " ".join(cards.split()).count("based on predicted quality and cost.") == 2
        for hidden in ("ug-smart-router:", "toolu_", "�"):
            assert hidden not in cards, cards
        for line_index, line in enumerate(terminal.screen.display):
            if "↳ Selected Model: claude-haiku-5-5" in line:
                style = terminal.screen.buffer[line_index][line.index("claude-haiku-5-5")]
                assert style.bold and style.fg != "default", style
        terminal.exit_normally()
    finally:
        (tmp_path / "routing-card-screen.txt").write_text(terminal.visible)
        (tmp_path / "routing-card-terminal.ansi").write_text("".join(terminal.output))
        terminal.child.kill()
