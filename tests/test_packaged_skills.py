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


def test_copies_every_skill_to_both_harness_directories(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, "smart-router")
    _write_skill(source, "second-skill")
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)

    installed = packaged_skills.install_packaged_skills(tmp_path / "home")

    assert installed == [
        tmp_path / "home/.claude/skills/second-skill",
        tmp_path / "home/.agents/skills/second-skill",
        tmp_path / "home/.claude/skills/smart-router",
        tmp_path / "home/.agents/skills/smart-router",
    ]
    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version one"
        assert destination.joinpath("references/details.md").read_text() == "details"


def test_reinstall_overwrites_existing_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = _write_skill(source, "smart-router", "version one")
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = packaged_skills.install_packaged_skills(home)
    skill.joinpath("SKILL.md").write_text("version two")

    packaged_skills.install_packaged_skills(home)

    assert all(path.joinpath("SKILL.md").read_text() == "version two" for path in installed)


def test_ignores_non_skill_entries(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("not a skill")
    (source / "incomplete").mkdir()
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)

    assert packaged_skills.install_packaged_skills(tmp_path / "home") == []


def test_missing_packaged_skills_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: tmp_path / "missing")

    with pytest.raises(RuntimeError, match="skill resources are missing"):
        packaged_skills.install_packaged_skills(tmp_path / "home")


def test_uninstalls_one_skill_from_both_harnesses(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, "smart-router")
    _write_skill(source, "second-skill")
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    packaged_skills.install_packaged_skills(home)

    removed = packaged_skills.uninstall_packaged_skill("smart-router", home)

    assert removed == [
        home / ".claude/skills/smart-router",
        home / ".agents/skills/smart-router",
    ]
    assert all(not path.exists() for path in removed)
    assert home.joinpath(".claude/skills/second-skill/SKILL.md").is_file()
    assert home.joinpath(".agents/skills/second-skill/SKILL.md").is_file()
    assert packaged_skills.uninstall_packaged_skill("smart-router", home) == []


@pytest.mark.parametrize("skill_name", ["../smart-router", "smart/router", "SmartRouter", ""])
def test_rejects_invalid_skill_name(tmp_path, skill_name):
    with pytest.raises(ValueError, match="Invalid skill name"):
        packaged_skills.uninstall_packaged_skill(skill_name, tmp_path)
