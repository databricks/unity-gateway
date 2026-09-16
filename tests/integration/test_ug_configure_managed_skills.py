"""Managed-config CUJ: the launched agent's /skills view lists the admin's downloaded skills.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so the real /skills TUI can be
driven against a skill the live workspace's published config does not include; only the config INPUT
is stubbed (auth, the skill download, the config writers, and the agent binary stay real). These
assert what the agent presents, not the bundles on disk (that is unit tests' job). See
tests/AGENTS.md rule 4. Claude reads ~/.claude/skills and Codex reads the shared ~/.agents/skills,
both of which `ug configure` writes, so a managed skill reaches either agent.
"""

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
CODEX_MODEL = "system.ai.gpt-5-6-sol"
# A real finalized skill in the managed e2e workspace's `main.default`. `ug configure` downloads it
# to disk, so its appearance in the agent's /skills view can only come from the injected config.
# `main.default.forkable-meals` names the securable; `forkable-meals` is the bundle name the agent
# lists (they match for this skill). Update if the workspace's skills change.
SKILL_FQN = "main.default.forkable-meals"
SKILL_NAME = "forkable-meals"
SKILLS_LOCATION = "main.default"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_skills_lists_downloaded_skill(live_session, workspace, tmp_path):
    """Scenario: configure Claude under an injected config that names a managed skill, open /skills.

    Expected: the downloaded managed skill (named via the `skills.names` selector) appears in the
    agent's /skills view.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        build_claude_agent_config([CLAUDE_OPUS]),
        skill_names=[SKILL_FQN],
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-skills") as tui:
        tui.boot()
        tui.send("/skills", "type the /skills command")
        tui.send("\r", "open the skills list")
        tui.wait_for(
            lambda s: SKILL_NAME in s,
            "the /skills view to list the downloaded managed skill",
            timeout=60,
        )


@pytest.mark.managed_fixture
@pytest.mark.codex
def test_managed_fixture_codex_skills_lists_downloaded_skill(live_session, workspace, tmp_path):
    """Scenario: configure Codex under an injected config with a managed skills schema, open /skills.

    Expected: the downloaded managed skill (from the `skills.unity_catalog_location` selector)
    appears in Codex's /skills view. Codex reads the shared ~/.agents/skills dir `ug configure`
    writes, so the admin's skill reaches it too.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config([CODEX_MODEL]),
        skills_location=SKILLS_LOCATION,
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "managed-skills") as tui:
        tui.boot()
        tui.send("/skills", "type the /skills command")
        tui.send("\r", "open the skills list")
        tui.send("\r", "confirm the skills selection so the list renders")
        tui.wait_for(
            lambda s: SKILL_NAME in s,
            "the /skills view to list the downloaded managed skill",
            timeout=60,
        )
