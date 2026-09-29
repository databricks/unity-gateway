"""Tests for the Smart Router skill shipped with Unity Gateway."""

from pathlib import Path

import pytest

from ucode import smart_router_skill


def _write_skill(root: Path, content: str = "version one") -> Path:
    skill = root / "smart-router"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    references = skill / "references"
    references.mkdir()
    (references / "details.md").write_text("details")
    return skill


def test_copies_skill_to_both_harness_directories(tmp_path, monkeypatch):
    source = _write_skill(tmp_path / "source")
    monkeypatch.setattr(smart_router_skill, "_skill_source", lambda: source)

    installed = smart_router_skill.install_smart_router_skill(tmp_path / "home")

    assert installed == [
        tmp_path / "home/.claude/skills/smart-router",
        tmp_path / "home/.agents/skills/smart-router",
    ]
    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version one"
        assert destination.joinpath("references/details.md").read_text() == "details"


def test_reinstall_overwrites_existing_files(tmp_path, monkeypatch):
    source = _write_skill(tmp_path / "source", "version one")
    monkeypatch.setattr(smart_router_skill, "_skill_source", lambda: source)
    home = tmp_path / "home"
    installed = smart_router_skill.install_smart_router_skill(home)
    source.joinpath("SKILL.md").write_text("version two")

    smart_router_skill.install_smart_router_skill(home)

    assert all(path.joinpath("SKILL.md").read_text() == "version two" for path in installed)


def test_missing_packaged_skill_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(smart_router_skill, "_skill_source", lambda: tmp_path / "missing")

    with pytest.raises(RuntimeError, match="skill resource is missing"):
        smart_router_skill.install_smart_router_skill(tmp_path / "home")
