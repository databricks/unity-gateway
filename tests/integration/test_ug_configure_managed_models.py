"""Managed-config CUJ: the launched agent's /model picker reflects the admin's model list.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so the real /model TUI can be
driven against a model list the live workspace does not publish; only the config INPUT is stubbed
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
# A real ca-central model absent from the live published config: its presence in the picker can
# only come from the injected config, which the live workspace's model list cannot produce.
CLAUDE_OFF_MENU = "system.ai.claude-sonnet-5"
LIVE_ONLY = "haiku-4-5"  # published live, but not in the injected list below


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_model_picker_reflects_the_config(live_session, workspace, tmp_path):
    """Scenario: launch Claude under an injected managed config and open the /model picker.

    Expected: the picker offers the injected models (including one the live workspace does not
    publish) and omits a model the live workspace does publish.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config([CLAUDE_OPUS, CLAUDE_OFF_MENU])
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-model") as tui:
        tui.boot()
        tui.send("/model", "type the /model command")
        tui.send("\r", "open the model picker")
        tui.wait_for(
            lambda s: "sonnet-5" in s, "the /model picker to list the injected model", timeout=60
        )
        assert LIVE_ONLY not in tui.visible, tui.visible
