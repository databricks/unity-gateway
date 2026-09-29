"""Tests for skills packaged with Unity Gateway."""

import multiprocessing
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


def _hold_installation_lock(base, entered, release):
    with packaged_skills._installation_lock(Path(base)):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("timed out waiting to release the installation lock")


def _enter_installation_lock(base, entered):
    with packaged_skills._installation_lock(Path(base)):
        entered.set()


def test_smart_router_skill_uses_launcher_specific_flags():
    content = (Path(__file__).resolve().parents[1] / "skills/smart-router/SKILL.md").read_text()

    for launcher in ("claude", "codex"):
        assert f"ug {launcher} --enable-smart-routing" in content
        assert f"ug {launcher} --disable-smart-routing" in content
    assert "ug smart-router" not in content


def test_installation_lock_serializes_concurrent_installers(tmp_path):
    context = multiprocessing.get_context("spawn")
    first_entered = context.Event()
    release_first = context.Event()
    second_entered = context.Event()
    first = context.Process(
        target=_hold_installation_lock,
        args=(str(tmp_path), first_entered, release_first),
    )
    second = context.Process(
        target=_enter_installation_lock,
        args=(str(tmp_path), second_entered),
    )
    first.start()
    try:
        assert first_entered.wait(timeout=5)
        second.start()
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
    finally:
        release_first.set()
        for process in (first, second):
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert second_entered.is_set()
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_installation_lock_times_out_when_holder_hangs(tmp_path):
    context = multiprocessing.get_context("spawn")
    holder_entered = context.Event()
    release_holder = context.Event()
    holder = context.Process(
        target=_hold_installation_lock,
        args=(str(tmp_path), holder_entered, release_holder),
    )
    holder.start()
    try:
        assert holder_entered.wait(timeout=5)
        with pytest.raises(RuntimeError, match="another Unity Gateway process"):
            with packaged_skills._installation_lock(tmp_path, timeout_seconds=0.05):
                pytest.fail("contended lock should not be acquired")
    finally:
        release_holder.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert holder.exitcode == 0


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


def test_reinstall_skips_unchanged_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    def fail_replace(*_args, **_kwargs):
        pytest.fail("unchanged skills should not be replaced")

    monkeypatch.setattr(packaged_skills, "_replace_skill", fail_replace)

    installed_again = packaged_skills.install_packaged_skills(
        packaged_skills.SMART_ROUTER_SKILL, home
    )

    assert installed_again == installed


def test_copy_failure_preserves_existing_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source_skill = _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)
    source_skill.joinpath("SKILL.md").write_text("version two")

    def fail_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(packaged_skills.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="disk full"):
        packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version one"


def test_swap_failure_restores_existing_skill(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source_skill = _write_skill(source, packaged_skills.SMART_ROUTER_SKILL)
    monkeypatch.setattr(packaged_skills, "_skills_source", lambda: source)
    home = tmp_path / "home"
    installed = packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)
    source_skill.joinpath("SKILL.md").write_text("version two")
    original_replace = Path.replace

    def fail_staged_swap(path, target):
        if path.name == "staged":
            raise OSError("swap failed")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_staged_swap)

    with pytest.raises(OSError, match="swap failed"):
        packaged_skills.install_packaged_skills(packaged_skills.SMART_ROUTER_SKILL, home)

    for destination in installed:
        assert destination.joinpath("SKILL.md").read_text() == "version one"


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
