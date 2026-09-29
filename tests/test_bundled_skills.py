"""Tests for skills bundled with Unity Gateway."""

import hashlib
import json
import shlex
from pathlib import Path

from ucode import bundled_skills


def _write_skill(root, name, content="# skill", **files):
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(content)
    for relative_path, body in files.items():
        path = skill / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return skill


def test_installs_every_skill_and_its_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(
        source,
        "smart-router",
        "__UG_EXECUTABLE__ __UG_LAUNCHER__ --enable-smart-routing\n",
        **{"references/details.md": "details"},
    )
    _write_skill(source, "second-skill")
    monkeypatch.setattr(bundled_skills, "_skills_root", lambda: source)
    home = tmp_path / "home"

    def render(_name, launcher, content):
        return content.replace(
            b"__UG_EXECUTABLE__", shlex.quote("/checkout with spaces/ug").encode()
        ).replace(b"__UG_LAUNCHER__", launcher.encode())

    installed = bundled_skills.install_bundled_skills(home, renderer=render)

    assert len(installed) == 4
    claude = home / ".claude/skills/smart-router"
    codex = home / ".agents/skills/smart-router"
    assert claude.joinpath("SKILL.md").read_text() == (
        "'/checkout with spaces/ug' claude --enable-smart-routing\n"
    )
    assert "codex --enable-smart-routing" in codex.joinpath("SKILL.md").read_text()
    assert claude.joinpath("references/details.md").read_text() == "details"
    assert home.joinpath(".claude/skills/second-skill/SKILL.md").is_file()


def test_upgrades_owned_skill_without_leaving_removed_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    skill = _write_skill(source, "example", "version one", **{"old.md": "old"})
    monkeypatch.setattr(bundled_skills, "_skills_root", lambda: source)
    home = tmp_path / "home"
    installed = bundled_skills.install_bundled_skills(home)

    skill.joinpath("SKILL.md").write_text("version two")
    skill.joinpath("old.md").unlink()
    bundled_skills.install_bundled_skills(home)

    assert all(path.joinpath("SKILL.md").read_text() == "version two" for path in installed)
    assert all(not path.joinpath("old.md").exists() for path in installed)


def test_preserves_unowned_name_collision(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    _write_skill(source, "example")
    monkeypatch.setattr(bundled_skills, "_skills_root", lambda: source)
    home = tmp_path / "home"
    collision = home / ".claude/skills/example"
    collision.mkdir(parents=True)
    collision.joinpath("SKILL.md").write_text("user authored\n")

    installed = bundled_skills.install_bundled_skills(home)

    assert collision not in installed
    assert collision.joinpath("SKILL.md").read_text() == "user authored\n"
    assert "Kept existing" in capsys.readouterr().out


def test_revert_preserves_modified_owned_copy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, "example")
    monkeypatch.setattr(bundled_skills, "_skills_root", lambda: source)
    home = tmp_path / "home"
    installed = bundled_skills.install_bundled_skills(home)
    claude_copy = next(path for path in installed if ".claude" in path.parts)
    codex_copy = next(path for path in installed if ".agents" in path.parts)
    claude_copy.joinpath("SKILL.md").write_text("user edit\n")

    results = bundled_skills.revert_bundled_skills(home)

    assert results == {claude_copy: False, codex_copy: True}
    assert claude_copy.exists()
    assert not codex_copy.exists()


def test_upgrades_legacy_single_file_manifest(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write_skill(source, "example", "version two")
    monkeypatch.setattr(bundled_skills, "_skills_root", lambda: source)
    home = tmp_path / "home"
    installs = {}
    for relative_root, _launcher in bundled_skills._INSTALL_TARGETS:
        installed = _write_skill(home / relative_root, "example", "version one")
        installs[str(installed)] = hashlib.sha256(b"version one").hexdigest()
    manifest = bundled_skills._manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": 1, "installs": installs}))

    bundled_skills.install_bundled_skills(home)

    assert all(
        path.joinpath("SKILL.md").read_text() == "version two" for path in map(Path, installs)
    )
    assert json.loads(manifest.read_text())["version"] == bundled_skills.MANIFEST_VERSION


def test_revert_ignores_manifest_paths_outside_skill_roots(tmp_path, capsys):
    home = tmp_path / "home"
    victim = _write_skill(tmp_path, "victim")
    digest = bundled_skills._digest(bundled_skills._installed_bundle(victim) or {})
    manifest = bundled_skills._manifest_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps({"version": bundled_skills.MANIFEST_VERSION, "installs": {str(victim): digest}})
    )

    bundled_skills.revert_bundled_skills(home)

    assert victim.exists()
    assert "unsafe bundled skill manifest path" in capsys.readouterr().out
