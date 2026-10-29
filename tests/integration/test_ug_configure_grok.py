"""CUJs: configure Grok Build through ug, use its real TUI, and revert."""

import tomllib

import pytest
from utils.agents import grok
from utils.constants import GROK_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk
from utils.terminal import AgentTerminal, ConfigureTerminal

pytestmark = [pytest.mark.live, pytest.mark.grok]

GROK_CONFIG = ".ucode/grok-home/config.toml"
MODEL_SOURCE_PROMPT = "How should Grok Build get its models?"


def _configure_command(session, workspace):
    return [
        str(session.binary),
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    ]


def _grok_config(session) -> dict:
    return tomllib.loads((session.home / GROK_CONFIG).read_text(encoding="utf-8"))


@pytest.mark.tui
def test_ug_configure_grok_databricks(live_session, workspace):
    """Scenario: pick Grok Build in `ug configure`'s agent picker and choose Databricks Hosted in
    its model-source picker (the e2e workspace also offers provider services as External Models),
    then use its TUI.

    Expected: configure succeeds with Databricks Hosted models; `ug grok` opens Grok without an
    xAI sign-in (it reports the API-key login ug's session secret provides), a file task's value
    appears in an assistant message of Grok's own saved chat history, it exits normally and can
    reopen; no file under the session home holds the Databricks credential.
    """
    session = live_session
    task = FileTask(session)
    command = _configure_command(session, workspace)
    with ConfigureTerminal(session, "grok", command, "configure") as configure:
        configure.select_agent("Grok Build")
        configure.choose(MODEL_SOURCE_PROMPT, "Databricks Hosted")
        configure.finish(timeout=240)
    state = session.workspace_state()
    assert "grok" in state["available_tools"]
    assert not state.get("provider_services", {}).get("grok")
    assert "Grok Build" in session.run("status").stdout

    command = [str(session.binary), "grok"]
    with AgentTerminal(session, "grok", command, "first-session") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    assert_no_bearer_on_disk(session)

    with AgentTerminal(session, "grok", command, "reopen") as tui:
        tui.boot()
        tui.check_input_and_exit()


def test_ug_configure_grok_anthropic_mps(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: choose External Models and the real API-key Anthropic Model Provider Service in
    `ug configure`'s pickers for Grok Build, then run a headless task with a bare `ug grok`.

    Expected: ug saves the provider and `ug status` shows it; the bare launch registers the
    service's models (no `system.ai` ones) and completes a file task on its starting model
    through the session proxy.
    """
    session = live_session
    task = FileTask(session)
    command = _configure_command(session, workspace)
    with ConfigureTerminal(session, "grok", command, "configure-provider") as configure:
        configure.select_agent("Grok Build")
        configure.choose(MODEL_SOURCE_PROMPT, "External Models")
        configure.choose("Select a model provider service:", claude_provider)
        configure.finish(timeout=240)
    state = session.workspace_state()
    assert state["provider_services"]["grok"] == claude_provider
    assert "grok" in state["available_tools"]
    assert claude_provider in session.run("status").stdout

    result = session.run("grok", "--", *GROK_HEADLESS_FLAGS, "-p", task.prompt, timeout=300)

    task.assert_headless_answer("grok", result)
    config = _grok_config(session)
    assert claude_provider_model in config["model"], config["model"]
    assert not any(name.startswith("system.ai.") for name in config["model"]), config["model"]
    assert grok.turn_models(grok.events(result.stdout)) == {config["models"]["default"]}


def test_ug_configure_grok_revert_keeps_the_users_grok_home(live_session, workspace):
    """Scenario: a user with their own Grok settings and MCP server in `~/.grok/config.toml`
    configures Grok Build and adds a Databricks MCP service through ug, then runs `ug revert`.

    Expected: ug's models and MCP entry live only in its private `~/.ucode/grok-home`, so the
    user's file is byte-identical throughout; revert removes ug's private config and still
    leaves the user's file unchanged.
    """
    session = live_session
    user_config = session.home / ".grok/config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(
        '[ui]\ntheme = "dark"\n\n[mcp_servers.mine]\ncommand = "mine-server"\nargs = []\n',
        encoding="utf-8",
    )
    original = user_config.read_bytes()

    session.run(
        "configure",
        "--agents",
        "grok",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    session.run("mcp", "add", "--names", "system.ai.dbsql", "--agents", "grok", timeout=240)
    config = _grok_config(session)
    assert config["model"], "ug wrote no Grok models"
    assert set(config["mcp_servers"]) == {"system-ai-dbsql"}
    assert user_config.read_bytes() == original

    session.run("revert", timeout=120)

    assert not (session.home / GROK_CONFIG).exists()
    assert user_config.read_bytes() == original
