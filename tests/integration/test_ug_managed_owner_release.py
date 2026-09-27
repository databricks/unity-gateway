"""Owner handoff and release through the installed integration command."""

import json
import sys
import tomllib
from pathlib import Path

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
)
from utils.terminal import AgentTerminal, TerminalProcess


@pytest.mark.installation
def test_ug_owner_release_without_application_preserves_user_settings(session):
    """Scenario: release an integration owner twice before UG has configured either agent.

    Expected: both installed entry points succeed without workspace authentication,
    setup or agent launch, leaving ordinary user settings unchanged.
    """
    claude = session.home / ".claude/settings.json"
    codex = session.home / ".codex/config.toml"
    claude.parent.mkdir()
    codex.parent.mkdir()
    claude.write_text('{"env":{"UG_USER_SETTING":"keep"}}\n')
    codex.write_text("[notice]\nhide_rate_limit_model_nudge = true\n")
    before = {path: path.read_bytes() for path in (claude, codex)}
    for entry_point in ("ug", "ucode"):
        for agent in ("claude", "codex"):
            result = session.run(
                "managed-config",
                "release",
                "--owner",
                "integration-owner",
                "--agent",
                agent,
                binary=session.binary.with_name(entry_point),
                timeout=30,
            )
            assert "Released integration-owner" in result.stdout
    assert {path: path.read_bytes() for path in before} == before
    assert not (session.home / ".ucode/state.json").exists()
    assert not (session.home / ".ucode/managed-config.json").exists()
    assert not (session.home / ".claude/ucode-settings.json").exists()
    assert not (session.home / ".codex/ucode.config.toml").exists()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.claude
def test_ug_claude_file_owner_adoption_and_release(live_session, workspace):
    """Scenario: hand off an equal Claude model contribution, complete a task, and release.

    Expected: the declared file owner adopts the existing contribution and release
    removes it together with generated UG routing/auth, preserving an unrelated env
    value. Repeating release changes neither private nor OS-managed settings.
    """
    session = live_session
    private = session.home / ".claude/ucode-settings.json"
    private.parent.mkdir()
    private.write_text(
        json.dumps(
            {
                "availableModels": [CLAUDE_TEST_MODEL],
                "env": {"UG_USER_SETTING": "keep"},
            }
        )
    )
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config([CLAUDE_TEST_MODEL])
    )
    config["handoff"] = {
        "schema_version": 1,
        "owner": "integration-owner",
        "migration_version": 1,
        "agents": {
            "claude": {
                "adopt": [
                    {
                        "target": "private_settings",
                        "path": ["availableModels"],
                        "elements": [CLAUDE_TEST_MODEL],
                    }
                ]
            }
        },
    }
    source = session.cwd / "handoff.json"
    source.write_text(json.dumps(config))
    task = FileTask(session)
    command = [str(session.binary), "claude", "--workspace", workspace, "-f", str(source)]
    with AgentTerminal(session, "claude", command, "owner-launch") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "claude")
    status = session.run("status").stdout
    assert "integration-owner" in status
    assert "File-managed (applied)" in status
    managed = Path(
        "/Library/Application Support/ClaudeCode/managed-settings.json"
        if sys.platform == "darwin"
        else "/etc/claude-code/managed-settings.json"
    )
    assert "apiKeyHelper" in json.loads(managed.read_text())
    release = [
        str(session.binary),
        "managed-config",
        "release",
        "--owner",
        "integration-owner",
        "--agent",
        "claude",
    ]
    with TerminalProcess(session, "ug", release, "owner-release") as terminal:
        terminal.finish()
    assert json.loads(private.read_text()) == {"env": {"UG_USER_SETTING": "keep"}}
    policy = json.loads(managed.read_text())
    assert "apiKeyHelper" not in policy
    assert "ANTHROPIC_BASE_URL" not in policy.get("env", {})
    assert "availableModels" not in policy
    before = (private.read_bytes(), managed.read_bytes())
    with TerminalProcess(session, "ug", release, "owner-repeat-release") as terminal:
        terminal.finish()
    assert (private.read_bytes(), managed.read_bytes()) == before


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.codex
def test_ug_codex_file_owner_release_removes_provider_and_catalog(live_session, workspace):
    """Scenario: launch Codex with a stable file owner, complete a task, then release it.

    Expected: release removes generated provider/auth and persistent catalog pointers
    from private, shared and OS-managed destinations while retaining an unrelated
    native user preference. Repeated release is safe.
    """
    session = live_session
    private = session.home / ".codex/ucode.config.toml"
    private.parent.mkdir()
    private.write_text("[notice]\nhide_rate_limit_model_nudge = true\n")
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX", build_codex_agent_config(models=[CODEX_TEST_MODEL])
    )
    config["handoff"] = {
        "schema_version": 1,
        "owner": "integration-owner",
        "migration_version": 1,
        "agents": {"codex": {}},
    }
    source = session.cwd / "handoff.json"
    source.write_text(json.dumps(config))
    task = FileTask(session)
    command = [str(session.binary), "codex", "--workspace", workspace, "-f", str(source)]
    with AgentTerminal(session, "codex", command, "owner-launch") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    managed = Path("/etc/codex/managed_config.toml")
    assert "Databricks" in tomllib.loads(managed.read_text())["model_providers"]
    assert (session.home / ".ucode/codex-model-catalog.json").is_file()
    release = [
        str(session.binary),
        "managed-config",
        "release",
        "--owner",
        "integration-owner",
        "--agent",
        "codex",
    ]
    for suffix in ("first", "repeat"):
        with TerminalProcess(session, "ug", release, f"owner-release-{suffix}") as terminal:
            terminal.finish()
        assert tomllib.loads(private.read_text()) == {
            "notice": {"hide_rate_limit_model_nudge": True}
        }
        for path in (managed, session.home / ".codex/config.toml"):
            document = tomllib.loads(path.read_text())
            assert "Databricks" not in document.get("model_providers", {})
            assert "model_catalog_json" not in document
            assert "model_provider" not in document
        assert not list((session.home / ".ucode").glob("codex-model-catalog*.json"))
