"""Tests for skills shipped with Unity Gateway."""

import re
import shlex
from pathlib import Path

import pytest

from ucode import skills
from ucode.smart_routing import orchestrator, session_env


def _write_skill(root: Path, name: str, content: str = "version one") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    references = skill / "references"
    references.mkdir()
    (references / "details.md").write_text("details")
    return skill


@pytest.fixture
def bundled_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = _write_skill(source, skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    return skill, tmp_path / "home"


@pytest.mark.parametrize(
    ("agent", "root"), [("claude", ".claude/skills"), ("codex", ".codex/skills")]
)
@pytest.mark.parametrize("agent_specific", [False, True])
def test_copies_named_skill_to_agent_directory(bundled_skill, agent, root, agent_specific):
    source_skill, home = bundled_skill
    _write_skill(source_skill.parent, "second-skill")
    if agent_specific:
        source_skill.joinpath("SKILL.md").unlink()
        for name in ("claude", "codex"):
            source_skill.joinpath(f"SKILL.{name}.md").write_text(f"{name} instructions")

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, agent, home)

    assert installed == home / root / "smart-router"
    expected = f"{agent} instructions" if agent_specific else "version one"
    assert installed.joinpath("SKILL.md").read_text() == expected
    assert installed.joinpath("references/details.md").read_text() == "details"
    assert not home.joinpath(root, "second-skill").exists()


@pytest.mark.parametrize("entrypoint", ["SKILL.md", "SKILL.codex.md"])
def test_reinstall_replaces_changed_bundle_and_skips_unchanged_bundle(
    bundled_skill, monkeypatch, entrypoint
):
    source_skill, home = bundled_skill
    source_skill.joinpath(entrypoint).write_text("version one")
    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    installed.joinpath("stale.txt").write_text("remove me")
    source_skill.joinpath(entrypoint).write_text("version two")

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed.joinpath("SKILL.md").read_text() == "version two"
    assert not installed.joinpath("stale.txt").exists()
    assert list(installed.parent.iterdir()) == [installed]

    def fail_copy(*_args, **_kwargs):
        pytest.fail("unchanged skills should not be replaced")

    monkeypatch.setattr(skills.shutil, "copytree", fail_copy)

    installed_again = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed_again == installed


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "payload",
    [
        {"hook_event_name": "UserPromptSubmit"},
        {"hook_event_name": "SessionStart", "source": "compact"},
    ],
)
def test_orchestrator_injects_the_installed_agent_instructions(
    bundled_skill, monkeypatch, agent, payload
):
    source_skill, home = bundled_skill
    source = _write_skill(source_skill.parent, skills.SMART_ROUTER_ORCHESTRATOR_SKILL)
    source.joinpath("SKILL.md").unlink()
    for name in ("claude", "codex"):
        source.joinpath(f"SKILL.{name}.md").write_text(f"{name} instructions")
    installed = skills.install_skill(skills.SMART_ROUTER_ORCHESTRATOR_SKILL, agent, home)
    monkeypatch.setenv("ENABLE_SMART_ROUTER_ORCHESTRATOR", "1")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    session_env.start_session()

    settings = {}
    orchestrator.sync_hooks(settings, agent=agent)
    hook = settings["hooks"][payload["hook_event_name"]][0]["hooks"][0]
    assert shlex.split(hook["command"])[-2:] == ["--agent", agent]
    output = orchestrator.hook_output(payload, agent=agent)

    assert output is not None
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.endswith(f"\n\n{agent} instructions")
    assert context.endswith(installed.joinpath("SKILL.md").read_text())


def test_orchestrator_skill_version_reads_the_shipped_skill():
    assert re.fullmatch(r"\d+\.\d+\.\d+", orchestrator.skill_version(agent="claude") or "")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('---\nname: x\nmetadata:\n  version: "1.2.0"\n---\nbody', "1.2.0"),
        ("---\nmetadata:\n  owner: ug\n  version: 2.0\n---\n", "2.0"),
        # Only metadata's version counts, and only inside the frontmatter.
        ("---\nversion: 9\nmetadata:\n  owner: ug\n---\nmetadata:\n  version: 9\n", None),
        ('---\nmetadata:\n  version: ""\n---\n', None),
        ("no frontmatter", None),
    ],
)
def test_orchestrator_skill_version_is_the_frontmatter_metadata_version(
    bundled_skill, text, expected
):
    source_skill, _home = bundled_skill
    _write_skill(source_skill.parent, skills.SMART_ROUTER_ORCHESTRATOR_SKILL, text)

    assert orchestrator.skill_version(agent="claude") == expected


def test_orchestrator_skill_version_follows_the_agent_entrypoint(bundled_skill):
    source_skill, _home = bundled_skill
    source = _write_skill(
        source_skill.parent,
        skills.SMART_ROUTER_ORCHESTRATOR_SKILL,
        "---\nmetadata:\n  version: 1.0.0\n---\n",
    )
    source.joinpath("SKILL.claude.md").write_text("---\nmetadata:\n  version: 2.0.0\n---\n")

    assert orchestrator.skill_version(agent="claude") == "2.0.0"
    assert orchestrator.skill_version(agent="codex") == "1.0.0"


def test_orchestrator_skill_version_is_none_without_the_skill(bundled_skill):
    assert orchestrator.skill_version(agent="claude") is None


def test_uninstalls_one_skill_from_agent_roots(bundled_skill):
    source_skill, home = bundled_skill
    _write_skill(source_skill.parent, "second-skill")
    skills.install_skill(skills.SMART_ROUTER_SKILL, "claude", home)
    skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)
    skills.install_skill("second-skill", "codex", home)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == [
        home / ".claude/skills/smart-router",
        home / ".codex/skills/smart-router",
    ]
    assert all(not path.exists() for path in removed)
    assert home.joinpath(".codex/skills/second-skill/SKILL.md").is_file()
    assert skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home) == []


def test_uninstall_unlinks_all_global_aliases_without_removing_targets(tmp_path):
    home = tmp_path / "home"
    aliases = [
        home / root / skills.SMART_ROUTER_SKILL
        for root in (".claude/skills", ".codex/skills", ".agents/skills")
    ]
    targets = [tmp_path / "targets" / str(index) for index in range(len(aliases))]
    for target in targets[:-1]:
        target.mkdir(parents=True)
    for alias, target in zip(aliases, targets, strict=True):
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == aliases
    assert all(not alias.is_symlink() for alias in aliases)
    assert all(target.is_dir() for target in targets[:-1])
    assert not targets[-1].exists()


def test_rejects_invalid_install_requests(tmp_path):
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.install_skill("../smart-router", "codex", tmp_path)
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.uninstall_skill("../smart-router", tmp_path)
    with pytest.raises(ValueError, match="Unsupported skill agent"):
        skills.install_skill(skills.SMART_ROUTER_SKILL, "other", tmp_path)
