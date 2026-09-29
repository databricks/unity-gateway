"""Tests for skills packaged with Unity Gateway."""

from pathlib import Path

import pytest

from ucode import packaged_skills


def _write_skill(root: Path, name: str, content: str = "version one") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    references = skill / "references"
    references.mkdir()
    (references / "details.md").write_text("details")
    return skill


def test_copies_named_skill_to_both_harness_directories(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    _write_skill(source, "second-skill")
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"

    installed = packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    assert installed == [
        home / ".claude/skills/smart-router",
        home / ".agents/skills/smart-router",
    ]
    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version one"
        assert destination.joinpath("references/details.md").read_text() == "details"
    assert not home.joinpath(".claude/skills/second-skill").exists()
    assert not home.joinpath(".agents/skills/second-skill").exists()


def test_reinstall_replaces_existing_skill_contents(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source_skill = _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    for root in (".claude/skills", ".agents/skills"):
        installed = home / root / packaged_skills.SMART_ROUTER_SKILL
        installed.joinpath("stale.txt").write_text("remove me")
    source_skill.joinpath("SKILL.md").write_text("version two")

    installed = packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version two"
        assert not destination.joinpath("stale.txt").exists()


def test_uninstalls_one_skill_from_both_harnesses(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    _write_skill(source, "second-skill")
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)
    packaged_skills.install_packaged_skills("second-skill", home)

    removed = packaged_skills.uninstall_packaged_skill(packaged_skills.SMART_ROUTER_SKILL, home)

    assert removed == [
        home / ".claude/skills/smart-router",
        home / ".agents/skills/smart-router",
    ]
    assert all(not path.exists() for path in removed)
    assert home.joinpath(".claude/skills/second-skill/SKILL.md").is_file()
    assert home.joinpath(".agents/skills/second-skill/SKILL.md").is_file()
    assert packaged_skills.uninstall_packaged_skill(packaged_skills.SMART_ROUTER_SKILL, home) == []


def test_rejects_unsafe_skill_names(tmp_path):
    with pytest.raises(ValueError, match="Invalid skill name"):
        packaged_skills.install_packaged_skills("../smart-router", tmp_path)
    with pytest.raises(ValueError, match="Invalid skill name"):
        packaged_skills.uninstall_packaged_skill("../smart-router", tmp_path)
