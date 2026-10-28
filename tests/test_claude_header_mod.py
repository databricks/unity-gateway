"""ug's Claude header mod: install beside the managed file, enable it there, and revert it."""

import copy
import json

from ucode import managed_files
from ucode.agents import claude
from ucode.managed_files import CLAUDE_HEADER_MOD_FILES, CLAUDE_HEADER_MOD_ROOTS, OS

WS = "https://example.cloud.databricks.com"


def test_configure_installs_and_enables_mod_then_stays_put(
    tmp_path, monkeypatch, claude_managed_env
):
    managed_path, root, writes = claude_managed_env()
    state = {"workspace": WS, "codex_models": []}

    claude.write_tool_config(state, "databricks-claude-sonnet-4")

    for path, text in claude._header_mod_files(root).items():
        assert path.read_text() == text
    doc = json.loads(managed_path.read_text())
    assert doc["extraKnownMarketplaces"]["ucode-managed"] == {
        "source": {"source": "directory", "path": str(root)}
    }
    assert doc["enabledPlugins"] == {"ug-headers@ucode-managed": True}
    # The built-in guard is the default list, so naming the mod there must keep it.
    assert doc["prependPlugins"] == ["ug-headers@ucode-managed", "sec-default@builtin"]
    assert claude.header_mod_active()

    count = len(writes)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert len(writes) == count  # unchanged files are never rewritten through sudo


def test_launch_never_installs_or_enables_the_mod(tmp_path, monkeypatch, claude_managed_env):
    managed_path, root, writes = claude_managed_env()
    monkeypatch.setattr(claude, "_explicit_configure", False)
    state = {"workspace": WS, "codex_models": []}

    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert not root.exists()
    assert "enabledPlugins" not in json.loads(managed_path.read_text())

    # Headers alone don't make a launch write root-owned files.
    monkeypatch.setenv("UG_HEADERS", "x-databricks-traffic-id: testenv://liteswap/unit")
    count = len(writes)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert not root.exists()
    assert "enabledPlugins" not in json.loads(managed_path.read_text())
    assert len(writes) == count
    assert not claude.header_mod_active()


def test_launch_leaves_a_stale_mod_alone_and_reads_it_as_inactive(
    tmp_path, monkeypatch, claude_managed_env
):
    managed_path, root, writes = claude_managed_env()
    state = {"workspace": WS, "codex_models": []}
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert claude.header_mod_active()

    # A ug upgrade changes register.ts; a launch must not rewrite the root-owned copy.
    register = root / "ug-headers/hooks/register.ts"
    register.write_text("// old version\n", encoding="utf-8")
    monkeypatch.setattr(claude, "_explicit_configure", False)
    monkeypatch.setenv("UG_HEADERS", "x-databricks-traffic-id: testenv://liteswap/unit")
    count = len(writes)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert register.read_text(encoding="utf-8") == "// old version\n"
    assert len(writes) == count
    assert not claude.header_mod_active()

    # Configure upgrades it.
    monkeypatch.setattr(claude, "_explicit_configure", True)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert claude.header_mod_active()


def test_admin_prepend_list_is_kept_and_revert_restores_it(
    tmp_path, monkeypatch, claude_managed_env
):
    managed_path, _root, _writes = claude_managed_env({"prependPlugins": ["acme-guard@acme"]})

    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    assert json.loads(managed_path.read_text())["prependPlugins"] == [
        "ug-headers@ucode-managed",
        "acme-guard@acme",
    ]

    claude.revert_managed_settings()
    assert json.loads(managed_path.read_text()) == {"prependPlugins": ["acme-guard@acme"]}


def test_mod_entries_never_count_as_conflicts():
    existing = {"prependPlugins": ["acme-guard@acme"], "enabledPlugins": {"x@y": True}}
    desired = claude._enable_header_mod(copy.deepcopy(existing), CLAUDE_HEADER_MOD_ROOTS[OS.LINUX])
    assert (
        claude._managed_settings_conflicts(existing, desired, claude.CLAUDE_HEADER_MOD_PATHS) == []
    )


def test_mod_files_are_on_the_sudo_allowlist():
    for os_family, root in CLAUDE_HEADER_MOD_ROOTS.items():
        files = set(claude._header_mod_files(root))
        assert files == {root / name for name in CLAUDE_HEADER_MOD_FILES}
        assert files <= managed_files._SUDO_REPLACE_TARGETS[os_family]


def test_unreadable_mod_folder_reads_as_inactive(tmp_path, monkeypatch):
    # A folder the user can't enter (an earlier install under umask 027) must warn, not crash.
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: tmp_path)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: tmp_path / "managed.json")

    def denied(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(claude.Path, "is_file", denied)
    assert claude.header_mod_active() is False


def test_failed_install_never_enables_a_missing_plugin(tmp_path, monkeypatch, claude_managed_env):
    managed_path, root, _writes = claude_managed_env()
    write = managed_files._sudo_replace

    def refuse_mod_files(target, text):
        if root in target.parents:
            raise PermissionError("denied")
        write(target, text)

    monkeypatch.setattr(managed_files, "_sudo_replace", refuse_mod_files)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")

    doc = json.loads(managed_path.read_text())
    assert not {"extraKnownMarketplaces", "enabledPlugins", "prependPlugins"} & set(doc)
    assert not claude.header_mod_active()


def test_revert_removes_the_mod_files_and_folders(tmp_path, monkeypatch, claude_managed_env):
    _managed_path, root, _writes = claude_managed_env()
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    assert (root / "ug-headers/hooks/register.ts").is_file()

    claude.revert_managed_settings()

    assert not root.exists()


def test_revert_keeps_the_guard_when_an_admin_extended_the_list(
    tmp_path, monkeypatch, claude_managed_env
):
    managed_path, _root, _writes = claude_managed_env()
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    doc = json.loads(managed_path.read_text())
    doc["prependPlugins"].append("acme-guard@acme")
    managed_path.write_text(json.dumps(doc), encoding="utf-8")

    claude.revert_managed_settings()

    # A non-empty list without the guard would turn it off, so revert keeps it.
    assert json.loads(managed_path.read_text())["prependPlugins"] == [
        "sec-default@builtin",
        "acme-guard@acme",
    ]


def test_header_mod_active_needs_every_file(tmp_path, monkeypatch, claude_managed_env):
    _managed_path, root, _writes = claude_managed_env()
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    assert claude.header_mod_active()

    (root / "ug-headers/.claude-plugin/plugin.json").unlink()
    assert not claude.header_mod_active()
