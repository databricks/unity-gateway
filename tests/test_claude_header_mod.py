"""ug's Claude header mod: install beside the managed file, enable it there, and revert it."""

import copy
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ucode import managed_files
from ucode.agents import claude
from ucode.managed_files import CLAUDE_HEADER_MOD_FILES, CLAUDE_HEADER_MOD_ROOTS, OS

WS = "https://example.cloud.databricks.com"
_REAL_WINDOWS_REPLACE = managed_files._windows_replace
_REAL_MANAGED_WRITES_ALLOWED = managed_files.managed_writes_allowed


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

    def _write(target, text, **_kwargs):
        writes.append(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(managed_files, "_sudo_replace", _write)
    monkeypatch.setattr(managed_files, "_sudo_remove", lambda path: path.unlink())
    monkeypatch.setattr(claude, "remove_privileged_tree", shutil.rmtree)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: root)
    monkeypatch.setattr(claude, "managed_writes_allowed", lambda **_kwargs: True)
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


def test_launch_never_installs_or_enables_the_mod(tmp_path, monkeypatch):
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
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


def test_launch_leaves_a_stale_mod_alone_and_reads_it_as_inactive(tmp_path, monkeypatch):
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
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
    managed_path = tmp_path / "managed.json"
    managed_path.write_text(
        json.dumps(claude._enable_header_mod({}, tmp_path / "ucode-plugins")), encoding="utf-8"
    )
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)

    def denied(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(claude.Path, "is_file", denied)
    assert claude.header_mod_active() is False


def test_failed_install_never_enables_a_missing_plugin(tmp_path, monkeypatch):
    managed_path, root, _writes = _managed_env(tmp_path, monkeypatch)
    write = managed_files._sudo_replace

    def refuse_mod_files(target, text, **kwargs):
        if root in target.parents:
            raise PermissionError("denied")
        write(target, text, **kwargs)

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


def _headless(monkeypatch):
    """No terminal, so writes go through ``sudo -n``: only where sudo needs no password."""
    monkeypatch.setattr(claude, "managed_writes_allowed", _REAL_MANAGED_WRITES_ALLOWED)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)


def test_headless_configure_installs_and_enables_beside_an_existing_managed_file(
    tmp_path, monkeypatch
):
    # `ug configure --agents claude,codex` from a provisioning script, with passwordless sudo.
    managed_path, root, _writes = _managed_env(tmp_path, monkeypatch, {})
    _headless(monkeypatch)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")

    assert (root / "ug-headers/hooks/register.ts").is_file()
    doc = json.loads(managed_path.read_text())
    assert doc["enabledPlugins"] == {"ug-headers@ucode-managed": True}
    # Only the mod's entries: a headless run still leaves the rest of ug's settings out.
    assert set(doc) == {"extraKnownMarketplaces", "enabledPlugins", "prependPlugins"}
    assert claude.header_mod_active()


def test_headless_configure_skips_the_mod_without_a_managed_file(tmp_path, monkeypatch):
    # Headless sudo only repairs a managed file in place, so there would be nothing to enable it in.
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
    _headless(monkeypatch)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")

    assert not root.exists()
    assert not managed_path.exists()
    assert writes == []


def test_headless_configure_without_passwordless_sudo_points_at_a_terminal(
    tmp_path, monkeypatch, capsys
):
    managed_path, root, _writes = _managed_env(tmp_path, monkeypatch, {})
    _headless(monkeypatch)
    write = managed_files._sudo_replace

    def password_required(target, text, **kwargs):
        if root in target.parents:
            raise subprocess.CalledProcessError(1, ["sudo", "-n"], stderr="a password is required")
        write(target, text, **kwargs)

    monkeypatch.setattr(managed_files, "_sudo_replace", password_required)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4")

    assert "run `ug configure` from a terminal" in " ".join(capsys.readouterr().out.split())
    assert "enabledPlugins" not in json.loads(managed_path.read_text())


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramW6432", str(tmp_path / "Program Files"))
    monkeypatch.setattr(managed_files, "current_os", lambda: OS.WINDOWS)
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)
    monkeypatch.setattr(managed_files, "_windows_replace", _REAL_WINDOWS_REPLACE)
    return tmp_path / "Program Files" / "ClaudeCode" / "ucode-plugins"


def test_windows_installs_and_removes_the_mod_beside_the_managed_file(windows, monkeypatch):
    assert managed_files.claude_header_mod_root() == windows
    files = claude._header_mod_files(windows)

    managed_files.reconcile_privileged_files(files, display="Claude Code")
    for path, text in files.items():
        assert path.read_text(encoding="utf-8") == text

    managed_files.remove_privileged_tree(windows)
    assert not windows.exists()

    monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    assert managed_files.claude_header_mod_root() is None


def test_windows_writes_only_the_mod_files_and_needs_an_administrator(windows, monkeypatch):
    with pytest.raises(RuntimeError, match="unexpected managed-settings target"):
        managed_files._windows_replace(windows / "other.json", "{}")
    with pytest.raises(RuntimeError, match="unexpected folder"):
        managed_files.remove_privileged_tree(windows.parent)

    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: False)
    with pytest.raises(PermissionError):
        managed_files.reconcile_privileged_files(
            claude._header_mod_files(windows), display="Claude Code"
        )


def _another_tools_mod(tmp_path, monkeypatch):
    """Managed settings enabling Isaac's copy of the mod, from its own marketplace folder."""
    marketplace = tmp_path / "isaac-plugins"
    (marketplace / "ug-headers/hooks").mkdir(parents=True)
    (marketplace / "ug-headers/hooks/hooks.json").write_text("{}", encoding="utf-8")
    managed_path = tmp_path / "managed-settings.json"
    # ug's own entry comes first, so its folder is checked before Isaac's.
    doc = claude._enable_header_mod({}, tmp_path / "ucode-plugins")
    doc["extraKnownMarketplaces"]["isaac-managed"] = {
        "source": {"source": "directory", "path": str(marketplace)}
    }
    doc["enabledPlugins"]["ug-headers@isaac-managed"] = True
    managed_path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    return marketplace


def test_another_tools_header_mod_counts_even_when_os_settings_are_skipped(tmp_path, monkeypatch):
    # Isaac launches ug with UG_SKIP_OS_SETTINGS=1 and enables its own copy of the mod.
    marketplace = _another_tools_mod(tmp_path, monkeypatch)
    monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    assert claude.header_mod_active()

    # An empty folder isn't a mod.
    (marketplace / "ug-headers/hooks/hooks.json").unlink()
    assert not claude.header_mod_active()


def test_an_unreadable_mod_folder_does_not_hide_another_tools_mod(tmp_path, monkeypatch):
    _another_tools_mod(tmp_path, monkeypatch)
    is_file = claude.Path.is_file

    def denied_in_ug_folder(self):
        if "ucode-plugins" in self.parts:
            raise PermissionError(13, "Permission denied", str(self))
        return is_file(self)

    monkeypatch.setattr(claude.Path, "is_file", denied_in_ug_folder)
    assert claude.header_mod_active()


def test_missing_mod_warning_matches_why_ug_cant_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: tmp_path)
    monkeypatch.setattr(claude, "current_os", lambda: OS.LINUX)
    assert "Run `ug configure` to install it" in claude._header_mod_missing_warning()

    monkeypatch.setattr(claude, "current_os", lambda: OS.WINDOWS)
    assert "`ug configure` as an administrator" in claude._header_mod_missing_warning()

    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: None)
    assert "on this platform" in claude._header_mod_missing_warning()

    monkeypatch.setenv("UG_SKIP_OS_SETTINGS", "1")
    message = claude._header_mod_missing_warning()
    assert "UG_SKIP_OS_SETTINGS" in message and "enable its header mod" in message
