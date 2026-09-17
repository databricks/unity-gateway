"""Managed-config CUJ: the launched agent's /mcp view lists the admin's MCP servers.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so the real /mcp TUI can be
driven against an MCP list the live workspace does not publish; only the config INPUT is stubbed
(auth, the config writers, and the agent binary stay real). These assert what the agent presents,
not the generated config files (that is unit tests' job). See tests/AGENTS.md rule 4.
"""

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
# A real ca-central MCP service; the live published config lists no MCP servers, so its appearance
# in the /mcp view can only come from the injected config.
MCP_SERVICE = "system.ai.github"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_mcp_lists_configured_server(live_session, workspace, tmp_path):
    """Scenario: launch Claude under an injected config with a managed MCP server and open /mcp.

    Expected: the injected MCP server appears in the agent's /mcp view.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        build_claude_agent_config([CLAUDE_OPUS]),
        mcp_names=[MCP_SERVICE],
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "managed config is published" in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-mcp") as tui:
        tui.boot()
        tui.send("/mcp", "type the /mcp command")
        tui.send("\r", "open the MCP list")
        tui.wait_for(
            lambda s: "github" in s.lower(),
            "the /mcp view to list the managed MCP server",
            timeout=60,
        )
