"""CUJs: real first-prompt routing and explicit-model bypass in Codex's TUI."""

import pytest
from utils.evidence import FileTask
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


def test_smart_routing_codex_first_prompt(live_session, workspace):
    """Scenario: enable smart routing and submit Codex's first interactive prompt.

    Expected: a real gateway decision selects a model and Codex completes the
    file-reading task before a normal exit. A routing fallback does not pass.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspaces",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )

    command = [str(session.binary), "codex", "--enable-smart-routing"]
    with AgentTerminal(session, "codex", command, "first-prompt") as tui:
        tui.boot()
        assert "[ROUTE]" not in session.routing_log("codex"), "Boot must not route a prompt"
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        log = session.routing_log("codex")
        assert "[ROUTE] selected" in log, log
        assert "selection failed" not in log and "settings/update timed out" not in log, log
        tui.exit_normally()
    task.assert_completed(session, "codex")
    with AgentTerminal(session, "codex", command, "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_smart_routing_codex_explicit_model_bypasses_routing(live_session, workspace):
    """Scenario: request an explicit model while launching an interactive routing session.

    Expected: codex completes the real task with the caller's model choice,
    starts no routing wrapper, exits normally, and reopens with usable input.
    """
    session = live_session
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspaces",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    model = session.model_for_explicit_case("codex")
    command = [str(session.binary), "codex", "--enable-smart-routing", "--", "--model", model]
    with AgentTerminal(session, "codex", command, "explicit-model") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")
    session.assert_not_routed()
    with AgentTerminal(session, "codex", command, "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()
    session.assert_not_routed()
