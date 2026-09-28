"""Direct local-policy launches through installed ug and real agent binaries."""

import json
import time
import tomllib

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
)
from utils.model_discovery import claude_model_in_picker
from utils.terminal import AgentTerminal


@pytest.mark.installation
def test_ug_and_ucode_claude_invalid_file_preserves_fresh_home(session):
    """Scenario: both installed entry points launch Claude with invalid explicit input.

    Expected: invalid JSON and a missing file fail before bootstrap or saved state,
    without starting Claude or using workspace configuration as a fallback.
    """
    source = session.cwd / "rendered policy.json"
    source.write_text("null", encoding="utf-8")
    for name in ("ug", "ucode"):
        for path in (source.name, "missing.json"):
            result = session.run(
                "claude",
                "--workspace",
                "https://workspace.example.invalid",
                "-f",
                path,
                binary=session.binary.with_name(name),
                ok=False,
                timeout=30,
            )
            assert result.returncode != 0
            assert "--config-file" in result.stdout + result.stderr
            assert "Traceback" not in result.stdout + result.stderr
            assert not (session.home / ".ucode").exists()
            assert not (session.home / ".claude").exists()


@pytest.mark.installation
def test_ug_and_ucode_codex_invalid_file_preserves_protocol_stdout(session):
    """Scenario: a client starts Codex app-server with invalid explicit policy.

    Expected: ug and ucode fail before setup, leave stdout empty, and create no
    UG or Codex state. No real agent or workspace credentials are required.
    """
    source = session.cwd / "rendered policy.json"
    source.write_text("{", encoding="utf-8")
    for name in ("ug", "ucode"):
        result = session.run(
            "codex",
            "--workspace",
            "https://workspace.example.invalid",
            "-f",
            source.name,
            "app-server",
            "--listen",
            "stdio://",
            binary=session.binary.with_name(name),
            ok=False,
            timeout=30,
            strip_ansi=False,
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert "--config-file" in result.stderr
        assert "Traceback" not in result.stderr
        assert not (session.home / ".ucode").exists()
        assert not (session.home / ".codex").exists()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.claude
def test_ug_claude_file_first_launch_and_immediate_update(live_session, workspace):
    """Scenario: launch Claude directly from a relative file, then update that file.

    Expected: first use needs no configure command, a real TUI file task completes,
    with ordinary model selection, then an authored model list takes effect within five minutes.
    Neither launch creates the API config cache. The input filename contains spaces.
    """
    session = live_session
    source = session.cwd / "rendered policy.json"
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {}},
    )
    source.write_text(json.dumps(config), encoding="utf-8")
    command = [str(session.binary), "claude", "--workspace", workspace, "-f", source.name]
    task = FileTask(session)
    assert not (session.home / ".ucode/state.json").exists()
    started = time.monotonic()
    with AgentTerminal(session, "claude", command, "file-first-launch") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    settings_path = session.home / ".claude/ucode-settings.json"
    settings = json.loads(settings_path.read_text())
    assert not {"availableModels", "enforceAvailableModels", "modelPicker"} & settings.keys()
    assert "ANTHROPIC_MODEL" not in settings["env"]
    assert session.workspace_state()["claude_models"]
    assert not (session.home / ".ucode/managed-config.json").exists()

    config["enabled_agents"] = [build_claude_agent_config([CLAUDE_TEST_MODEL])]
    source.write_text(json.dumps(config), encoding="utf-8")
    with AgentTerminal(session, "claude", command, "file-updated-launch") as tui:
        tui.boot()
        assert time.monotonic() - started < 300
        assert json.loads(settings_path.read_text())["availableModels"] == [CLAUDE_TEST_MODEL]
        screen = tui.open_model_picker(
            model_visible=lambda text: claude_model_in_picker(
                text, CLAUDE_TEST_MODEL, "Claude Haiku 4.5"
            )
        )
        assert claude_model_in_picker(screen, CLAUDE_TEST_MODEL, "Claude Haiku 4.5")
        assert "sonnet-4-6" not in screen
        tui.check_input_and_exit()
    assert not (session.home / ".ucode/managed-config.json").exists()
    session.assert_not_routed()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.codex
def test_ug_codex_file_first_launch_and_app_server_update(live_session, workspace):
    """Scenario: first-launch Codex from a file, update it, then start app-server.

    Expected: a real TUI file task completes without prior configure. The next
    launch applies an authored model catalog within five minutes and returns a
    clean real app-server model/list response. The API cache remains absent.
    """
    session = live_session
    source = session.cwd / "rendered policy.json"
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        {"agent": "CODING_AGENT_CODEX", "config": {}},
    )
    source.write_text(json.dumps(config), encoding="utf-8")
    command = [str(session.binary), "codex", "--workspace", workspace, "-f", source.name]
    task = FileTask(session)
    assert not (session.home / ".ucode/state.json").exists()
    started = time.monotonic()
    with AgentTerminal(session, "codex", command, "file-first-launch") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    settings = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
    assert not {"model", "model_reasoning_effort", "model_catalog_json"} & settings.keys()
    assert session.workspace_state()["codex_models"]
    assert not (session.home / ".ucode/managed-config.json").exists()

    config["enabled_agents"] = [build_codex_agent_config(models=[CODEX_TEST_MODEL])]
    source.write_text(json.dumps(config), encoding="utf-8")
    models = session.codex_model_ids(
        ["--workspace", workspace, "-f", source.name, "app-server", "--listen", "stdio://"],
        name="updated-file-catalog",
    )
    assert time.monotonic() - started < 300
    assert models == [CODEX_TEST_MODEL]
    assert not (session.home / ".ucode/managed-config.json").exists()
    session.assert_not_routed()
