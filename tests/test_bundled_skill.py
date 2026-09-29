"""Tests for the bundled Smart Router skill."""

from ucode.smart_routing import bundled_skill


def test_quotes_launching_ug_path(monkeypatch):
    monkeypatch.setattr(
        bundled_skill,
        "ug_binary",
        lambda: "/checkout with spaces/.venv/bin/ug",
    )

    content = bundled_skill._skill_content("codex")

    assert "'/checkout with spaces/.venv/bin/ug' codex --disable-smart-routing" in content


def test_installs_harness_specific_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_skill, "ug_binary", lambda: "/bin/ug")

    installed = bundled_skill.install_bundled_skill(tmp_path)

    assert len(installed) == 2
    claude_content = (tmp_path / ".claude/skills/smart-router/SKILL.md").read_text()
    codex_content = (tmp_path / ".agents/skills/smart-router/SKILL.md").read_text()
    assert "/bin/ug claude --enable-smart-routing" in claude_content
    assert "/bin/ug codex --enable-smart-routing" in codex_content
    assert 'version: "1.0.0"' in claude_content + codex_content


def test_upgrades_unchanged_owned_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_skill, "_skill_content", lambda _launcher: "version one\n")
    installed = bundled_skill.install_bundled_skill(tmp_path)

    monkeypatch.setattr(bundled_skill, "_skill_content", lambda _launcher: "version two\n")
    bundled_skill.install_bundled_skill(tmp_path)

    assert all((path / "SKILL.md").read_text() == "version two\n" for path in installed)


def test_preserves_unowned_name_collision(tmp_path, capsys):
    collision = tmp_path / ".claude/skills/smart-router"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user authored\n")

    installed = bundled_skill.install_bundled_skill(tmp_path)

    assert collision not in installed
    assert (collision / "SKILL.md").read_text() == "user authored\n"
    assert "Kept existing" in capsys.readouterr().out


def test_revert_preserves_modified_owned_copy(tmp_path):
    claude_copy, codex_copy = bundled_skill.install_bundled_skill(tmp_path)
    (claude_copy / "SKILL.md").write_text("user edit\n")

    results = bundled_skill.revert_bundled_skill()

    assert results == {claude_copy: False, codex_copy: True}
    assert claude_copy.exists()
    assert not codex_copy.exists()
