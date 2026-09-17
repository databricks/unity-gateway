"""CUJ: launch Claude through ug with custom OAuth handled by the Databricks CLI."""

import configparser
import json

import pytest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.claude]


@pytest.mark.smoke
def test_ug_claude_custom_oauth_cli_boots(live_session, workspace):
    """Scenario: launch Claude with the CLI OAuth flag and databricks-cli client ID.

    Expected: Databricks CLI 1.17.0 is installed; the real Claude TUI reaches a
    usable prompt, accepts keyboard input, and exits normally; its generated CLI
    profile records client_id=databricks-cli. This boot-only smoke check does not
    claim model inference.
    """
    session = live_session
    session.env["ENABLE_CUSTOM_OAUTH_FROM_CLI"] = "1"
    version = session.run("version", "--output", "json", binary="databricks")
    assert json.loads(version.stdout)["Version"] == "1.17.0"
    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--client-id",
        "databricks-cli",
    ]

    with AgentTerminal(session, "claude", command, "custom-oauth-cli") as tui:
        tui.boot(timeout=240)
        tui.check_input_and_exit()

    profiles = configparser.ConfigParser(interpolation=None)
    assert profiles.read(session.home / ".databrickscfg")
    profile = session.workspace_state()["custom_oauth"]["profile"]
    assert profiles[profile]["client_id"] == "databricks-cli"
