"""CUJs: configure Antigravity CLI through ug, use its real TUI, and revert."""

import json

import pytest
from utils.agents import antigravity
from utils.evidence import FileTask
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.antigravity]


@pytest.mark.tui
def test_ug_configure_antigravity_databricks(live_session, workspace):
    """Scenario: pick Antigravity CLI in `ug configure`'s agent picker and choose Databricks
    Hosted in its model-source picker (the e2e workspace also offers a Gemini Enterprise
    service as External Models), then use its TUI.

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
        configure.choose("How should Antigravity CLI get its models?", "Databricks Hosted")
        configure.finish(timeout=240)
    assert "antigravity" in session.workspace_state()["available_tools"]
    assert not session.workspace_state().get("provider_services", {}).get("antigravity")
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


def test_ug_configure_antigravity_gemini_mps(
    live_session, workspace, antigravity_provider, antigravity_provider_models
):
    """Scenario: choose External Models and the real Gemini Enterprise MPS in `ug configure`'s
    pickers for Antigravity CLI, then run a headless task with a bare `ug antigravity`.

    The service is allow-all (it serves any of its provider's models and declares none), so
    configure asks which model to start on; the user types the first listed model.

    Expected: ug saves the provider and the typed model; `ug status` shows the provider; the
    bare launch uses the saved choice to complete a file task through ug's proxy, every
    request naming that model.
    """
    session = live_session
    task = FileTask(session)
    model = antigravity_provider_models[0]
    command = [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "antigravity", command, "configure-provider") as configure:
        configure.select_agent("Antigravity CLI")
        configure.choose("How should Antigravity CLI get its models?", "External Models")
        configure.choose("Select a model provider service:", antigravity_provider)
        configure.wait_for(
            lambda text: "Model for Antigravity CLI to start on" in text,
            "the start-model prompt",
            timeout=120,
        )
        configure.submit(model)
        configure.finish(timeout=240)
    state = session.workspace_state()
    assert state["provider_services"]["antigravity"] == antigravity_provider
    assert "antigravity" in state["available_tools"]
    assert state["antigravity_provider_model"] == model
    assert antigravity_provider in session.run("status").stdout

    result = session.run(
        "antigravity", "--", "-p", task.prompt, *antigravity.HEADLESS_ARGS, timeout=300
    )

    task.assert_headless_answer("antigravity", result)
    assert antigravity.requested_models(session) == {model}


def test_ug_configure_antigravity_revert_restores_user_files(live_session, workspace):
    """Scenario: a user with their own Antigravity skills.json and MCP server configures
    Antigravity and adds a Databricks MCP service through ug, then runs `ug revert`.

    skills.json is shared with the Antigravity app and IDE, so the user also adds an entry of
    their own after configuring.

    Expected: while configured, ug's skills directory and MCP entry sit beside the user's own;
    after revert both files hold only the user's content, including the entry added after
    configure (ug removes its own entries in place rather than restoring a snapshot).
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
    configured = json.loads(skills.read_text())
    assert configured["entries"] == [
        {"path": str(session.home / "mine")},
        {"path": str(session.home / ".agents/skills")},
    ]
    assert set(json.loads(mcp.read_text())["mcpServers"]) == {"mine", "system-ai-dbsql"}
    later = {"path": str(session.home / "added-later")}
    configured["entries"].append(later)
    skills.write_text(json.dumps(configured, indent=2))

    session.run("revert", timeout=120)

    assert json.loads(skills.read_text()) == {
        "entries": [{"path": str(session.home / "mine")}, later]
    }
    assert json.loads(mcp.read_text()) == original_mcp
