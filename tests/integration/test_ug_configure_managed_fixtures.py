"""Managed-config CUJs that drive the real agent TUI under an injected admin config.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so we can exercise shapes the
live workspace does not publish; only that INPUT is stubbed (auth, the config writers, and the agent
binaries stay real). These assert what the launched agent actually presents in its TUI, the /model
picker and the /mcp list, not the generated config files (asserting the files is unit tests' job).
See tests/AGENTS.md rule 4.
"""

import json

import pytest
from utils.terminal import AgentTerminal

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
CLAUDE_SONNET = "system.ai.claude-sonnet-4-6"
CLAUDE_HAIKU = "system.ai.claude-haiku-4-5"
MCP_SERVICE = "system.ai.github"


def _claude_agent(models: list[str]) -> dict:
    return {
        "agent": "CODING_AGENT_CLAUDE_CODE",
        "config": {
            "models": {"model_services": models},
            "default_models": {"default_model": models[0]},
        },
    }


def _config(default_agent: str, *agents: dict, mcp_names: list[str] | None = None) -> dict:
    cfg = {"spec_version": 1, "default_agent": default_agent, "enabled_agents": list(agents)}
    if mcp_names is not None:
        cfg["mcp_servers"] = {"names": mcp_names}
    return cfg


def _apply(session, tmp_path, workspace, config: dict):
    stub = tmp_path / "managed-config.json"
    stub.write_text(json.dumps(config))
    session.env["UCODE_MANAGED_CONFIG_STUB"] = str(stub)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "managed config is published" in result.stdout, result.stdout


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_model_picker_lists_managed_models(
    live_session, workspace, tmp_path
):
    """Scenario: launch Claude under a managed config and open the /model picker.

    Expected: the picker offers the admin's managed models and not a model outside that list.
    """
    session = live_session
    models = [CLAUDE_OPUS, CLAUDE_HAIKU]  # distinct from the live config's three models
    _apply(session, tmp_path, workspace, _config("CODING_AGENT_CLAUDE_CODE", _claude_agent(models)))
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "managed-fixture-model"
    ) as tui:
        tui.boot()
        tui.send("/model", "type the /model command")
        tui.send("\r", "open the model picker")
        tui.wait_for(
            lambda s: "opus-4-8" in s and "haiku-4-5" in s,
            "the /model picker to list the managed models",
        )
        assert "sonnet-4-6" not in tui.visible, tui.visible


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_mcp_lists_configured_server(live_session, workspace, tmp_path):
    """Scenario: launch Claude under a managed config with an MCP server and open /mcp.

    Expected: the managed MCP server is listed in the agent's /mcp view.
    """
    session = live_session
    config = _config(
        "CODING_AGENT_CLAUDE_CODE", _claude_agent([CLAUDE_OPUS]), mcp_names=[MCP_SERVICE]
    )
    _apply(session, tmp_path, workspace, config)
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "managed-fixture-mcp"
    ) as tui:
        tui.boot()
        tui.send("/mcp", "type the /mcp command")
        tui.send("\r", "open the MCP list")
        tui.wait_for(
            lambda s: "github" in s.lower(), "the /mcp view to list the managed MCP server"
        )
