"""Managed-config CUJ: the launched agent's /skills view lists the admin's downloaded skills.

The admin CodingAgentConfig is a checked-in JSON fixture injected via UCODE_MANAGED_CONFIG_STUB so
the real /skills TUI can be driven against a managed skill; only the config INPUT is stubbed (auth, the skill download, the config writers, and the agent binary stay real). These
assert what the agent presents, not the bundles on disk (that is unit tests' job). See
tests/AGENTS.md rule 4. Claude reads ~/.claude/skills and Codex reads the shared ~/.agents/skills,
both of which `ug configure` writes, so a managed skill reaches either agent. The reconcile
lifecycle test additionally checks the on-disk bundles, since removal-on-reconcile is a disk change
that has no steady-state `/skills` signal.
"""

import pytest
from utils.managed import use_managed_config_fixture
from utils.terminal import AgentTerminal

# A real finalized skill in the managed e2e workspace's `main.default`, named by the skills fixtures
# (`main.default.forkable-meals`); `forkable-meals` is the bundle name the agent lists (they match
# for this skill). Update the fixtures and this name if the workspace's skills change.
SKILL_NAME = "forkable-meals"
SKILL_ROOTS = (".claude/skills", ".agents/skills")
# Bundle name for a hand-authored developer skill used in the reconcile lifecycle; it has no
# attribution record, so it stands in for any skill ug did not download.
DEVELOPER_SKILL = "developer-own"


def _bundles_on_disk(session) -> set[str]:
    """The skill bundle directory names under the user's shared `.claude/skills` root."""
    root = session.home / ".claude" / "skills"
    return {child.name for child in root.iterdir() if child.is_dir()} if root.exists() else set()


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_skills_lists_downloaded_skill(live_session, workspace):
    """Scenario: configure Claude under an injected config that names a managed skill, open /skills.

    Expected: the downloaded managed skill (named via the `skills.names` selector) appears in the
    agent's /skills view.
    """
    session = live_session
    use_managed_config_fixture(session, "claude_skills_by_name")
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
def test_managed_fixture_codex_skills_lists_downloaded_skill(live_session, workspace):
    """Scenario: configure Codex under an injected config with a managed skills schema, open /skills.

    Expected: the downloaded managed skill (from the `skills.unity_catalog_location` selector)
    appears in Codex's /skills view. Codex reads the shared ~/.agents/skills dir `ug configure`
    writes, so the admin's skill reaches it too.
    """
    session = live_session
    use_managed_config_fixture(session, "codex_skills_by_location")
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


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_skills_reconcile_lifecycle(live_session, workspace):
    """Scenario: managed skills download, coexist with a developer's own skill, then reconcile away.

    Expected: a `unity_catalog_location` config downloads the workspace's skills to disk; a
    developer's own hand-authored skill (no attribution record) sits alongside them; and a later
    no-skills config removes only the managed skills, leaving the developer's own untouched. This
    proves the reconcile is attribution-driven: it never deletes a skill ug did not download.
    """
    session = live_session

    def configure(fixture: str) -> None:
        use_managed_config_fixture(session, fixture)
        result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=300)
        assert "Select coding agents to configure:" not in result.stdout, result.stdout

    configure("claude_no_skills")
    assert _bundles_on_disk(session) == set(), "a no-skills config must download nothing"

    configure("claude_skills_by_location")
    managed_bundles = _bundles_on_disk(session)
    assert managed_bundles, "the managed skills should have been downloaded"
    for root in SKILL_ROOTS:
        for bundle in managed_bundles:
            assert (session.home / root / bundle).is_dir(), f"{root}/{bundle} missing"

    # A developer's own skill, hand-authored on disk with no attribution record.
    for root in SKILL_ROOTS:
        own = session.home / root / DEVELOPER_SKILL
        own.mkdir(parents=True, exist_ok=True)
        (own / "SKILL.md").write_text(
            f"---\nname: {DEVELOPER_SKILL}\ndescription: a developer's own skill.\n---\n"
        )

    configure("claude_no_skills")
    remaining = _bundles_on_disk(session)
    assert remaining == {DEVELOPER_SKILL}, remaining
    assert not (managed_bundles & remaining), "managed skills should have been reconciled away"
