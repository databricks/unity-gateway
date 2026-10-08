"""CUJ3 skills: a `unity_catalog_location` downloads every skill in the schema and nothing else."""

from __future__ import annotations

import re

import pytest

from .base import BaseCujTest
from .helpers.constants import (
    CLAUDE,
    CODEX,
    FIXTURE_CATALOG_LOOKUP_SKILL_NAME,
    FIXTURE_DECOY_SKILL_NAME,
    FIXTURE_NOTES_SKILL_NAME,
    FIXTURE_SUMMARY_SKILL_NAME,
    SKILLS_LOCATION,
)
from .helpers.skills import (
    SKILL_ROOTS,
    assert_fixture_summary_invoked,
    bundle_files,
    bundles_on_disk,
    skill_name,
    skills_view,
)

SCHEMA_SKILLS = tuple(
    map(
        skill_name,
        (FIXTURE_SUMMARY_SKILL_NAME, FIXTURE_NOTES_SKILL_NAME, FIXTURE_CATALOG_LOOKUP_SKILL_NAME),
    )
)
DECOY = skill_name(FIXTURE_DECOY_SKILL_NAME)
STATUS_PANEL_TITLES = {CLAUDE: "Claude Code", CODEX: "Codex"}


class TestCuj3Skills(BaseCujTest):
    WORKSPACE_URL = "https://dbc-bbdd5508-648e.cloud.databricks.com"

    @pytest.fixture(scope="class")
    def configured(self, cuj):
        session, workspace, recorder = cuj
        assert workspace.config()["skills"] == {"unity_catalog_location": SKILLS_LOCATION}
        session.configure(
            [
                "configure",
                "--workspace",
                workspace.url,
                "--skip-upgrade",
                "--disable-databricks-ai-tools",
            ]
        )
        # `ug status` takes no workspace flag, so read it before retargeting to the recorder.
        status = session.run("status", timeout=120).stdout
        recorder.retarget_session(session.home)
        return session, recorder, status

    def test_every_schema_skill_is_on_disk_and_the_decoy_is_not(self, configured):
        """Scenario: configure with `skills.unity_catalog_location` set to the skills schema.

        Expected: both roots hold exactly the schema's three skills with their supporting files,
        and nothing from the other schema.
        """
        session, _, _ = configured
        for root in SKILL_ROOTS:
            assert bundles_on_disk(session, root) == set(SCHEMA_SKILLS)
            assert "reference/fixture-facts.md" in bundle_files(session, root, SCHEMA_SKILLS[0])
            assert "scripts/lookup_schema.py" in bundle_files(session, root, SCHEMA_SKILLS[2])

    @pytest.mark.parametrize("agent", [CLAUDE, CODEX])
    def test_agent_lists_every_schema_skill_and_not_the_decoy(self, configured, agent):
        """Scenario: open /skills in each agent.

        Expected: all three schema skills are listed and the out-of-scope decoy is not.
        """
        session, recorder, _ = configured
        screen = skills_view(session, recorder, agent, SCHEMA_SKILLS, codex_filter="fixture")
        assert DECOY not in screen, f"{agent} /skills:\n{screen}"

    @pytest.mark.parametrize("agent", [CLAUDE, CODEX])
    def test_status_counts_the_skills(self, configured, agent):
        """Scenario: run `ug status` after configure.

        Expected: each agent's panel reports one Skills row counting the three schema skills.
        """
        *_, status = configured
        panel = re.search(
            rf"╭[─ ]*{STATUS_PANEL_TITLES[agent]}[─ ]*╮.*?Skills:\s+(\d+)", status, re.DOTALL
        )
        assert panel and int(panel.group(1)) == len(SCHEMA_SKILLS), status

    @pytest.mark.parametrize("agent", [CLAUDE, CODEX])
    def test_agent_invokes_a_downloaded_skill(self, configured, agent):
        """Scenario: ask each agent, headless, to use fixture-summary.

        Expected: the reply carries the token found only in the bundle's reference file, so
        listing alone is not the pass condition.
        """
        session, recorder, _ = configured
        assert_fixture_summary_invoked(session, recorder, agent)
