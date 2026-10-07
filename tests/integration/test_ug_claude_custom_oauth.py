"""CUJ: launch Claude through ug with custom OAuth handled by the Databricks CLI."""

import configparser
import json
import shlex
from urllib.parse import urlparse

import pytest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.claude]

CLIENT_ID = "databricks-cli"
MANAGED_SETTINGS_PATH = "/etc/claude-code/managed-settings.json"


@pytest.mark.smoke
def test_ug_claude_custom_oauth_cli_boots(live_session, workspace):
    """Scenario: launch Claude with the CLI OAuth flag and databricks-cli client ID.

    Expected: Databricks CLI 1.17.0 is installed; the real Claude TUI reaches a
    usable prompt, accepts keyboard input, and exits normally; its generated CLI
    profile records client_id=databricks-cli; and the OS-managed apiKeyHelper uses
    only the installed ug path, workspace, and generated profile. This boot-only
    smoke check does not claim model inference.
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
        CLIENT_ID,
    ]

    with AgentTerminal(session, "claude", command, "custom-oauth-cli") as tui:
        tui.boot(timeout=240)
        tui.check_input_and_exit()

    profiles = configparser.ConfigParser(interpolation=None)
    assert profiles.read(session.home / ".databrickscfg")
    profile = session.workspace_state()["custom_oauth"]["profile"]
    hostname = urlparse(workspace).hostname
    assert hostname
    assert profile == f"ug-oauth-{hostname}-{CLIENT_ID}"
    assert profiles[profile]["client_id"] == CLIENT_ID

    managed_settings = json.loads(
        session.run(MANAGED_SETTINGS_PATH, binary="cat", timeout=30).stdout
    )
    assert shlex.split(managed_settings["apiKeyHelper"]) == [
        str(session.binary.with_name("ug")),
        "auth-token",
        "--host",
        workspace,
        "--profile",
        profile,
    ]
