"""Tests for skills shipped with Unity Gateway."""

from pathlib import Path

import pytest

from ucode import skills


def _write_skill(root: Path, name: str, content: str = "version one") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    references = skill / "references"
    references.mkdir()
    (references / "details.md").write_text("details")
    return skill


@pytest.mark.parametrize(
    ("agent", "root"), [("claude", ".claude/skills"), ("codex", ".codex/skills")]
)
def test_copies_named_skill_to_agent_directory(tmp_path, monkeypatch, agent, root):
    source = tmp_path / "source"
    _write_skill(source, skills.SMART_ROUTER_SKILL)
    _write_skill(source, "second-skill")
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    home = tmp_path / "home"

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, agent, home)

    assert installed == home / root / "smart-router"
    assert installed.joinpath("SKILL.md").read_text() == "version one"
    assert installed.joinpath("references/details.md").read_text() == "details"
    assert not home.joinpath(root, "second-skill").exists()


def test_reinstall_replaces_existing_skill_contents(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source_skill = _write_skill(source, skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    installed.joinpath("stale.txt").write_text("remove me")
    source_skill.joinpath("SKILL.md").write_text("version two")

    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed.joinpath("SKILL.md").read_text() == "version two"
    assert not installed.joinpath("stale.txt").exists()
    assert list(installed.parent.iterdir()) == [installed]


def test_reinstall_skips_unchanged_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    def fail_copy(*_args, **_kwargs):
        pytest.fail("unchanged skills should not be replaced")

    monkeypatch.setattr(skills.shutil, "copytree", fail_copy)

    installed_again = skills.install_skill(skills.SMART_ROUTER_SKILL, "codex", home)

    assert installed_again == installed


def test_uninstalls_one_skill_from_agent_roots(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, skills.SMART_ROUTER_SKILL)
    _write_skill(source, "second-skill")
    monkeypatch.setattr(skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
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


@pytest.mark.parametrize("codex_target", ["claude", "shared", "missing"])
def test_uninstall_removes_codex_alias(tmp_path, codex_target):
    home = tmp_path / "home"
    claude_skill = _write_skill(home / ".claude/skills", skills.SMART_ROUTER_SKILL)
    codex_alias = home / ".codex/skills/smart-router"
    shared_skill = _write_skill(home / ".agents/skills", skills.SMART_ROUTER_SKILL)
    codex_alias.parent.mkdir(parents=True)
    targets = {
        "claude": claude_skill,
        "shared": shared_skill,
        "missing": home / ".missing/skills/smart-router",
    }
    codex_alias.symlink_to(targets[codex_target])

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == [claude_skill, codex_alias, shared_skill]
    assert not claude_skill.exists()
    assert not codex_alias.is_symlink()
    assert not shared_skill.exists()


def test_uninstall_removes_independent_codex_copy(tmp_path):
    home = tmp_path / "home"
    codex_skill = _write_skill(home / ".codex/skills", skills.SMART_ROUTER_SKILL)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == [codex_skill]
    assert not codex_skill.exists()


def test_uninstall_unlinks_all_global_aliases_without_removing_targets(tmp_path):
    home = tmp_path / "home"
    aliases = [
        home / root / skills.SMART_ROUTER_SKILL
        for root in (".claude/skills", ".codex/skills", ".agents/skills")
    ]
    targets = [tmp_path / "targets" / str(index) for index in range(len(aliases))]
    for alias, target in zip(aliases, targets, strict=True):
        target.mkdir(parents=True)
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target)

    removed = skills.uninstall_skill(skills.SMART_ROUTER_SKILL, home)

    assert removed == aliases
    assert all(not alias.is_symlink() for alias in aliases)
    assert all(target.is_dir() for target in targets)


def test_rejects_unsafe_skill_names(tmp_path):
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.install_skill("../smart-router", "codex", tmp_path)
    with pytest.raises(ValueError, match="Invalid skill name"):
        skills.uninstall_skill("../smart-router", tmp_path)


def test_rejects_unknown_agent(tmp_path):
    with pytest.raises(ValueError, match="Unsupported skill agent"):
        skills.install_skill(skills.SMART_ROUTER_SKILL, "other", tmp_path)
