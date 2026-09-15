"""CUJs: configure Codex through ug, then use its real interactive session."""

import pytest
from utils.evidence import FileTask
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


@pytest.mark.smoke
def test_ug_configure_codex_databricks(live_session, workspace):
    """Scenario: configure Codex with Databricks Hosted and use its TUI.

    Expected: configure succeeds, Codex returns a file value through the real
    gateway, exits normally, and can reopen the configuration ug created.
    Optional AI Tools are disabled; the selected agent version is kept pinned.
    """
    session = live_session
    task = FileTask(session)

    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspaces",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert not session.workspace_state().get("provider_services", {}).get("codex")

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "first-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_ug_configure_codex_openai_mps(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: choose the real OpenAI MPS in ug configure's provider picker.

    Expected: ug saves that provider, and launching Codex without --provider
    uses the saved choice with one of its allowed models to complete a
    file-reading task and exit normally.
    """
    session = live_session
    task = FileTask(session)

    command = [
        str(session.binary),
        "configure",
        "--workspaces",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]
    with ConfigureTerminal(session, "codex", command, "configure-provider") as configure:
        configure.select_agent("Codex")
        configure.choose("How should Codex get its models?", "External Models")
        configure.choose("Select a model provider service:", codex_provider)
        configure.finish(timeout=240)
    assert session.workspace_state()["provider_services"]["codex"] == codex_provider
    assert codex_provider in session.run("status").stdout

    command = [str(session.binary), "codex", "--", "--model", codex_provider_model]
    with AgentTerminal(session, "codex", command, "provider-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task, timeout=300)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()
