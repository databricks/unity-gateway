"""Managed-config CUJ: the launched agent's /skills view lists the admin's downloaded skills.

The admin CodingAgentConfig is a checked-in JSON fixture injected via UCODE_MANAGED_CONFIG_STUB so
the real /skills TUI can be driven against a managed skill; only the config INPUT is stubbed (auth, the skill download, the config writers, and the agent binary stay real). These
assert what the agent presents, not the bundles on disk (that is unit tests' job). See
tests/AGENTS.md rule 4. Claude reads ~/.claude/skills and Codex reads the shared ~/.agents/skills,
both of which `ug configure` writes, so a managed skill reaches either agent. The reconcile
lifecycle test additionally checks the on-disk bundles, since removal-on-reconcile is a disk change
that has no steady-state `/skills` signal.
"""

import json
import shutil

import pytest
from utils.evidence import FileTask
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


@pytest.mark.managed_fixture
@pytest.mark.codex
@pytest.mark.tui
def test_ug_codex_repairs_shared_project_skill_downloads(live_session, workspace):
    """Scenario: download one real UC skill into three projects, then remove one root in two.

    Expected: the first normal Codex launch repairs both partial installs, preserves the current
    install and every source attribution, and completes a real TUI file task. The normal ug debug
    log shows one successful metadata GET and one successful bundle-root directory GET. Only the admin
    config INPUT uses the existing fixture; skill APIs, downloads, state, and agent stay real.
    """
    session = live_session
    use_managed_config_fixture(session, "codex_skills_by_location")
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    source = f"main.default.{SKILL_NAME}"
    projects = [session.cwd / name for name in ("current", "repair-claude", "repair-codex")]
    for project in projects:
        project.mkdir()
        session.run("skills", "add", "--names", source, "--path", str(project), timeout=240)

    manifest_path = session.home / ".ucode/skills.json"
    before = json.loads(manifest_path.read_text())
    assert "last_update_check" not in before, "The first launch must exercise a due refresh"
    project_records = {
        record["base"]: record
        for record in before["skill_downloads"]
        if record["fqn"] == source and record["scope"] == "project"
    }
    assert set(project_records) == {str(project) for project in projects}
    for project in projects:
        assert project_records[str(project)]["dirs"] == [
            str(project / root / SKILL_NAME) for root in SKILL_ROOTS
        ]

    original = {}
    current_mtimes = {}
    for project in projects:
        for root in SKILL_ROOTS:
            bundle = project / root / SKILL_NAME
            files = {
                path.relative_to(bundle).as_posix(): path.read_bytes()
                for path in bundle.rglob("*")
                if path.is_file()
            }
            assert "SKILL.md" in files, f"{bundle} was not downloaded"
            if not original:
                original = files
            assert files == original
            if project == projects[0]:
                current_mtimes.update(
                    (path, path.stat().st_mtime_ns) for path in bundle.rglob("*") if path.is_file()
                )
    # Ordinary user deletions leave one surviving agent copy per project; ug must repair the pair.
    shutil.rmtree(projects[1] / SKILL_ROOTS[0] / SKILL_NAME)
    shutil.rmtree(projects[2] / SKILL_ROOTS[1] / SKILL_NAME)
    session.env["UCODE_DEBUG"] = "1"
    task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "shared-project-skill-refresh"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    task.assert_completed(session, "codex")

    for project in projects:
        for root in SKILL_ROOTS:
            bundle = project / root / SKILL_NAME
            assert {
                path.relative_to(bundle).as_posix(): path.read_bytes()
                for path in bundle.rglob("*")
                if path.is_file()
            } == original
    assert all(path.stat().st_mtime_ns == modified for path, modified in current_mtimes.items()), (
        "The already-current installation must not be rewritten"
    )

    after = json.loads(manifest_path.read_text())
    assert after["last_update_check"]
    before_records = {
        (record["base"], record["fqn"]): record for record in before["skill_downloads"]
    }
    after_records = {(record["base"], record["fqn"]): record for record in after["skill_downloads"]}
    assert after_records.keys() == before_records.keys()
    repaired = {str(project) for project in projects[1:]}
    for key, previous in before_records.items():
        current = after_records[key]
        if key[0] in repaired:
            assert {k: v for k, v in current.items() if k != "downloaded_at"} == {
                k: v for k, v in previous.items() if k != "downloaded_at"
            }
        else:
            assert current == previous

    # These are ordinary production debug messages from actual successful HTTP requests.
    debug = "\n".join(path.read_text() for path in (session.home / ".ucode").glob("debug.log*"))
    request_counts = {
        "metadata": sum(
            f"/api/2.1/unity-catalog/skills/{source}: HTTP 200," in line
            for line in debug.splitlines()
        ),
        "bundle_directory": sum(
            f"/api/2.0/fs/directories/Skills/main/default/{SKILL_NAME}: HTTP 200," in line
            for line in debug.splitlines()
        ),
    }
    session.record("shared-skill-refresh.json", {"source": source, "requests": request_counts})
    assert request_counts == {"metadata": 1, "bundle_directory": 1}
