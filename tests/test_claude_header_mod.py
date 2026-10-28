"""ug's Claude header mod: install beside the managed file, enable it there, and revert it."""

import copy
import json
import shutil
from types import SimpleNamespace

from ucode import managed_files
from ucode.agents import claude
from ucode.managed_files import CLAUDE_HEADER_MOD_FILES, CLAUDE_HEADER_MOD_ROOTS, OS

WS = "https://example.cloud.databricks.com"


def _managed_env(tmp_path, monkeypatch, managed_doc=None):
    """Real reconcile flow, with privileged writes landing in ``tmp_path`` and counted."""
    managed_path = tmp_path / "managed-settings.json"
    if managed_doc is not None:
        managed_path.write_text(json.dumps(managed_doc), encoding="utf-8")
    root = tmp_path / "ucode-plugins"
    backup_dir = tmp_path / "managed-backups"
    writes: list[str] = []
    monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
    monkeypatch.setattr(managed_files, "current_os", lambda: OS.LINUX)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json")

    def _write(target, text):
        writes.append(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(managed_files, "_sudo_replace", _write)
    monkeypatch.setattr(managed_files, "_sudo_remove", lambda path: path.unlink())
    monkeypatch.setattr(claude, "remove_privileged_tree", shutil.rmtree)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: root)
    monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
    monkeypatch.setattr(claude, "save_state", lambda state: None)
    monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
    monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "ucode-settings.json")
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda *a, **kw: SimpleNamespace(manifest={"claude": {}})
    )
    # As `ug configure` runs it.
    monkeypatch.setattr(claude, "_explicit_configure", True)
    monkeypatch.delenv("UG_HEADERS", raising=False)
    return managed_path, root, writes


def test_configure_installs_and_enables_mod_then_stays_put(tmp_path, monkeypatch):
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
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


def test_launch_adds_the_mod_only_with_headers_and_keeps_configures(tmp_path, monkeypatch):
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
    monkeypatch.setattr(claude, "_explicit_configure", False)
    state = {"workspace": WS, "codex_models": []}

    # Another writer of the managed file drops entries it doesn't know; a plain launch mustn't
    # add them back every time.
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert not root.exists()
    assert "enabledPlugins" not in json.loads(managed_path.read_text())

    monkeypatch.setenv("UG_HEADERS", "x-databricks-traffic-id: testenv://liteswap/unit")
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert claude.header_mod_active()

    # Once added, a plain launch leaves the mod as it is.
    monkeypatch.delenv("UG_HEADERS")
    count = len(writes)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    assert claude.header_mod_active() and len(writes) == count


def test_admin_prepend_list_is_kept_and_revert_restores_it(tmp_path, monkeypatch):
    managed_path, _root, _writes = _managed_env(
        tmp_path, monkeypatch, {"prependPlugins": ["acme-guard@acme"]}
    )

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


def test_failed_install_never_enables_a_missing_plugin(tmp_path, monkeypatch):
    managed_path, root, _writes = _managed_env(tmp_path, monkeypatch)
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


def test_revert_removes_the_mod_files_and_folders(tmp_path, monkeypatch):
    _managed_path, root, _writes = _managed_env(tmp_path, monkeypatch)
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    assert (root / "ug-headers/hooks/register.ts").is_file()

    claude.revert_managed_settings()

    assert not root.exists()


def test_revert_keeps_the_guard_when_an_admin_extended_the_list(tmp_path, monkeypatch):
    managed_path, _root, _writes = _managed_env(tmp_path, monkeypatch)
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


def test_header_mod_active_needs_every_file(tmp_path, monkeypatch):
    _managed_path, root, _writes = _managed_env(tmp_path, monkeypatch)
    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")
    assert claude.header_mod_active()

    (root / "ug-headers/.claude-plugin/plugin.json").unlink()
    assert not claude.header_mod_active()
