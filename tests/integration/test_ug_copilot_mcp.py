"""CUJ for Databricks MCP servers in GitHub Copilot CLI through installed ug."""

import json

import pytest
from utils.constants import CLAUDE_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import copilot_mcp_tool_calls
from utils.terminal import TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.copilot]

MCP_SERVICE = "system.ai.web_search"
# The developer's own server, disabled in Copilot's settings so the session never starts it.
USER_SERVER = "developer-own"
USER_ENTRY = {"type": "local", "command": "developer-own-mcp", "args": [], "tools": ["*"]}


def _copilot_mcp_servers(session) -> dict:
    """Copilot's own view of its configured MCP servers (`copilot mcp list --json`)."""
    result = session.run("mcp", "list", "--json", binary=session.which("copilot"), timeout=60)
    return json.loads(result.stdout)["mcpServers"]


def test_ug_mcp_add_copilot_registers_server_in_copilot_config(live_session, workspace):
    """Scenario: with a disabled server of their own in Copilot's user config, the developer
    configures Copilot, adds `system.ai.web_search` with `ug mcp add --agents copilot`, asks
    `ug copilot` to use it in a headless task, then runs `ug revert`.

    Expected: Copilot's own `copilot mcp list --json` lists ug's server from its user config
    next to the developer's still-disabled server; `ug mcp list --agents copilot` reports it
    enabled; Copilot's session events show a successful call to the server's tool, loaded from
    the user config; revert removes only ug's server, so Copilot lists just the developer's.
    Limits: the service needs no connection sign-in, so the sign-in path is not covered here.
    """
    session = live_session
    # Ordinary pre-existing user settings, not manufactured gateway state.
    user_config = session.home / ".copilot/mcp-config.json"
    user_config.parent.mkdir(parents=True)
    user_config.write_text(json.dumps({"mcpServers": {USER_SERVER: USER_ENTRY}}), encoding="utf-8")
    user_settings = session.home / ".copilot/settings.json"
    user_settings.write_text(json.dumps({"disabledMcpServers": [USER_SERVER]}), encoding="utf-8")

    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.run(
        "mcp", "add", "--agents", "copilot", "--workspace", workspace, "--names", MCP_SERVICE
    )
    [server] = [
        entry["name"]
        for entry in session.workspace_state()["mcp_servers"]
        if MCP_SERVICE in str(entry.get("url"))
    ]

    servers = _copilot_mcp_servers(session)
    assert servers[server]["source"] == "user", servers
    assert servers[server]["enabled"] is True, servers
    assert servers[USER_SERVER]["enabled"] is False, servers
    listing = session.run("mcp", "list", "--agents", "copilot").stdout
    rows = [line for line in listing.splitlines() if server in line]
    assert rows and all("enabled" in row for row in rows), listing

    session.record("copilot-model.json", {"model": CLAUDE_TEST_MODEL})
    session.run(
        "copilot",
        "--model",
        CLAUDE_TEST_MODEL,
        "-p",
        f"Call the `{server}` web_search tool once to search for 'Databricks Unity Catalog'. "
        "Then reply with the single word DONE.",
        *COPILOT_HEADLESS_FLAGS,
        timeout=240,
    )
    calls = [call for call in copilot_mcp_tool_calls(session) if call["server"] == server]
    assert calls, copilot_mcp_tool_calls(session)
    assert all(call["source"] == "user" for call in calls), calls
    assert any(call["succeeded"] for call in calls), calls

    with TerminalProcess(session, "ug", [str(session.binary), "revert"], "revert") as terminal:
        terminal.finish()
    assert set(_copilot_mcp_servers(session)) == {USER_SERVER}
    assert json.loads(user_config.read_text(encoding="utf-8")) == {
        "mcpServers": {USER_SERVER: USER_ENTRY}
    }
    settings = json.loads(user_settings.read_text(encoding="utf-8"))
    assert settings["disabledMcpServers"] == [USER_SERVER], settings
    assert not (session.home / ".copilot/ucode-mcp-config.json").exists()
