"""Explicit managed-agent selection with real configuration and inference.

Only the admin-authored CodingAgentConfig input is injected through the supported
managed_fixture hook. The workspace, installed ug/Claude, generated state, and task are real.
"""

import hashlib
import os
import shutil
from pathlib import Path

import pytest
from utils.evidence import FileTask
from utils.managed import use_managed_config_fixture
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed_fixture, pytest.mark.claude, pytest.mark.tui]


def _snapshot(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_ug_configure_selected_managed_claude_leaves_other_agents_unconfigured(
    live_session, workspace
):
    """Scenario: an admin enables Claude, Codex, and Pi; the user selects only Claude.

    Expected: public configure creates only Claude's agent configuration, leaves Codex's
    per-user config and Pi's generated files absent, and preserves Codex's OS-managed file
    (or its absence) and Pi's executable availability. The real Claude TUI completes a tiny
    file-reading task through the gateway before exiting normally.

    The fixture publishes no MCPs or skills; workspace-wide shared policy reconciliation
    is outside this installation/model-configuration selection check.
    """
    session = live_session
    use_managed_config_fixture(session, "selected_managed_agents")
    task = FileTask(session)
    codex_before = shutil.which("codex", path=session.env["PATH"])
    pi_before = shutil.which("pi", path=session.env["PATH"])
    if os.name == "nt":
        program_data = next(
            (value for name, value in session.env.items() if name.casefold() == "programdata"), None
        )
        assert program_data, "Windows session is missing ProgramData"
        codex_managed = Path(program_data) / "OpenAI/Codex/requirements.toml"
    else:
        codex_managed = Path("/etc/codex/managed_config.toml")
    codex_managed_before = _snapshot(codex_managed)

    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    assert session.workspace_state()["available_tools"] == ["claude"]
    assert (session.home / ".claude/ucode-settings.json").is_file()
    assert not (session.home / ".codex/ucode.config.toml").exists()
    assert _snapshot(codex_managed) == codex_managed_before, (
        "Unselected Codex managed settings changed"
    )
    assert shutil.which("codex", path=session.env["PATH"]) == codex_before
    assert shutil.which("pi", path=session.env["PATH"]) == pi_before
    assert not (session.home / ".ucode/pi-home/.pi/agent/models.json").exists()
    assert not (session.home / ".ucode/pi-home/.pi/agent/settings.json").exists()

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "selected-managed-claude"
    ) as terminal:
        terminal.boot()
        terminal.submit(task.prompt)
        terminal.wait_for_task(task, timeout=180)
        terminal.exit_normally()

    task.assert_completed(session, "claude")
