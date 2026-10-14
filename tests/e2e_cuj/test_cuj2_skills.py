"""CUJ2 skills: the one named managed skill is downloaded, listed, and invoked by both agents."""

from __future__ import annotations

import pytest

from ucode.skills_api import fetch_skill_bundle

from .helpers.constants import (
    CLAUDE,
    CODEX,
    FIXTURE_CATALOG_LOOKUP_SKILL_NAME,
    FIXTURE_NOTES_SKILL_NAME,
    SKILLS_LOCATION,
)
from .helpers.skills import (
    FIXTURE_SUMMARY,
    SKILL_ROOTS,
    assert_fixture_summary_invoked,
    bundle_files,
    bundles_on_disk,
    skill_name,
    skills_view,
)
from .test_cuj2_mps_mcp import _assert_cuj2_config, _Cuj2Base

CUJ_NAME = "CUJ 2 · Named skills"

OTHER_SKILLS = tuple(map(skill_name, (FIXTURE_NOTES_SKILL_NAME, FIXTURE_CATALOG_LOOKUP_SKILL_NAME)))


class TestCuj2Skills(_Cuj2Base):
    @pytest.fixture(scope="class")
    def configured(self, cuj):
        session, workspace, recorder = cuj
        _assert_cuj2_config(workspace.config())
        recorder.configure_session(
            session, ["configure", "--skip-upgrade", "--disable-databricks-ai-tools"]
        )
        return cuj

    def test_named_skill_matches_live_fixture_and_siblings_are_absent(self, configured):
        """Scenario: configure with `skills.names` selecting only fixture-summary.

        Expected: both roots hold exactly that bundle, byte-identical to the live fixture.
        """
        session, workspace, _ = configured
        live, reason = fetch_skill_bundle(
            workspace.url,
            session.env["DATABRICKS_BEARER"],
            *SKILLS_LOCATION.split("."),
            FIXTURE_SUMMARY,
        )
        assert live and "reference/fixture-facts.md" in live, reason
        for root in SKILL_ROOTS:
            assert bundles_on_disk(session, root) == {FIXTURE_SUMMARY}
            assert bundle_files(session, root, FIXTURE_SUMMARY) == live

    @pytest.mark.parametrize("agent", [CLAUDE, CODEX])
    def test_agent_lists_only_the_named_skill(self, configured, agent):
        """Scenario: open /skills in each agent.

        Expected: fixture-summary is listed and the schema's other skills are not.
        """
        session, _, recorder = configured
        screen = skills_view(session, recorder, agent, (FIXTURE_SUMMARY,))
        for other in OTHER_SKILLS:
            assert other not in screen, f"{agent} /skills:\n{screen}"

    @pytest.mark.parametrize("agent", [CLAUDE, CODEX])
    def test_agent_invokes_the_named_skill(self, configured, agent):
        """Scenario: ask each agent, headless, to use fixture-summary.

        Expected: the reply carries the token found only in the bundle's reference file, after a
        200 inference response through the recorder.
        """
        session, _, recorder = configured
        assert_fixture_summary_invoked(session, recorder, agent)
