"""CUJs: configure Antigravity CLI through ug, use its real TUI, and revert."""

import json

import pytest
from utils.agents import antigravity
from utils.evidence import FileTask
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.antigravity]


@pytest.mark.tui
def test_ug_configure_antigravity_databricks(live_session, workspace):
    """Scenario: pick Antigravity CLI in `ug configure`'s agent picker, then use its TUI.

    Expected: configure succeeds with Databricks Hosted models; `ug antigravity` opens
    Antigravity without Google sign-in, first-run onboarding is answered through its visible
    screens, and a file task's value appears in a finished agent response in Antigravity's own
    conversation store; it exits normally and can reopen.
    """
    session = live_session
    task = FileTask(session)
    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "antigravity", command, "configure") as configure:
        configure.select_agent("Antigravity CLI")
        configure.finish(timeout=240)
    assert "antigravity" in session.workspace_state()["available_tools"]
    assert "Antigravity CLI" in session.run("status").stdout

    command = [str(session.binary), "antigravity"]
    with AgentTerminal(session, "antigravity", command, "first-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    assert antigravity.gateway_authenticated(session)

    with AgentTerminal(session, "antigravity", command, "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_ug_configure_antigravity_revert_restores_user_files(live_session, workspace):
    """Scenario: a user with their own Antigravity skills.json and MCP server configures
    Antigravity and adds a Databricks MCP service through ug, then runs `ug revert`.

    Expected: while configured, ug's skills directory and MCP entry sit beside the user's own;
    after revert skills.json is the user's original file again (restored from ug's backup) and
    the MCP config holds only the user's server (ug removes its entry in place).
    """
    session = live_session
    config = session.home / ".gemini/config"
    config.mkdir(parents=True)
    skills = config / "skills.json"
    mcp = config / "mcp_config.json"
    skills.write_text(json.dumps({"entries": [{"path": str(session.home / "mine")}]}, indent=2))
    mcp.write_text(
        json.dumps({"mcpServers": {"mine": {"serverUrl": "https://example.com/mcp"}}}, indent=2)
    )
    original_skills = skills.read_bytes()
    original_mcp = json.loads(mcp.read_text())

    session.run(
        "configure",
        "--agents",
        "antigravity",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    session.run("mcp", "add", "--names", "system.ai.dbsql", "--agents", "antigravity", timeout=240)
    assert len(json.loads(skills.read_text())["entries"]) == 2
    assert set(json.loads(mcp.read_text())["mcpServers"]) == {"mine", "system-ai-dbsql"}

    session.run("revert", timeout=120)

    assert skills.read_bytes() == original_skills
    assert json.loads(mcp.read_text()) == original_mcp
