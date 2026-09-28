"""Tests for ucode.vscode: pointing the Claude Code VS Code extension at Unity Gateway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ucode import config_io, vscode

# Captured at import: the autouse fixture replaces vscode.vscode_installs so no other test can
# reach the developer's real VS Code settings.
from ucode.vscode import vscode_installs as real_vscode_installs

WRAPPER = "/tools/bin/ug-claude-vscode"
WRAPPER_KEY = vscode.WRAPPER_SETTING
LOGIN_KEY = vscode.DISABLE_LOGIN_SETTING
MODE_KEY = vscode.PERMISSION_MODE_SETTING


def _extensions_json(path: Path, *ids: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"identifier": {"id": ext_id}, "version": "1.0.0"} for ext_id in ids]
    path.write_text(json.dumps(entries), encoding="utf-8")


def _install(
    tmp_path,
    *,
    extension="anthropic.claude-code-2.1.282-win32-x64",
    name="VS Code",
    listed: tuple[str, ...] | None = None,
    profiles: list[dict] | None = None,
):
    """A VS Code install under tmp_path. ``listed`` writes the default profile's extensions.json."""
    root = tmp_path / name
    extensions_dir = root / "extensions"
    extensions_dir.mkdir(parents=True)
    if extension:
        (extensions_dir / extension).mkdir()
    if listed is not None:
        _extensions_json(extensions_dir / "extensions.json", *listed)
    user_dir = root / "User"
    if profiles is not None:
        storage = user_dir / "globalStorage" / "storage.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"userDataProfiles": profiles}), encoding="utf-8")
    return vscode.VSCodeInstall(name, extensions_dir, user_dir / "settings.json", user_dir)


def _write_settings(install, doc_or_text):
    install.settings_path.parent.mkdir(parents=True, exist_ok=True)
    text = doc_or_text if isinstance(doc_or_text, str) else json.dumps(doc_or_text, indent=4)
    install.settings_path.write_text(text, encoding="utf-8")


def _settings(install) -> dict:
    return json.loads(install.settings_path.read_text(encoding="utf-8"))


def _record() -> dict:
    return json.loads(
        (config_io.APP_DIR / "vscode-claude-extension.json").read_text(encoding="utf-8")
    )


def _configure(install):
    vscode.configure_claude_extension(installs=lambda: [install])


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setattr(vscode, "wrapper_executable", lambda: WRAPPER)
    return WRAPPER


@pytest.fixture
def messages(monkeypatch):
    captured: dict[str, list[str]] = {"warning": [], "note": [], "success": []}
    monkeypatch.setattr(vscode, "print_warning", captured["warning"].append)
    monkeypatch.setattr(vscode, "print_note", captured["note"].append)
    monkeypatch.setattr(vscode, "print_success", captured["success"].append)
    return captured


class TestInstallLocations:
    @pytest.fixture
    def home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        return tmp_path

    def _by_name(self):
        return {install.name: install for install in real_vscode_installs()}

    def test_windows_uses_appdata(self, monkeypatch, home):
        monkeypatch.setattr(vscode.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(home / "Roaming"))
        installs = self._by_name()
        assert (
            installs["VS Code"].settings_path
            == home / "Roaming" / "Code" / "User" / "settings.json"
        )
        assert installs["VS Code Insiders"].settings_path == (
            home / "Roaming" / "Code - Insiders" / "User" / "settings.json"
        )
        assert installs["VS Code"].extensions_dir == home / ".vscode" / "extensions"

    def test_macos_uses_application_support(self, monkeypatch, home):
        monkeypatch.setattr(vscode.sys, "platform", "darwin")
        assert self._by_name()["VS Code"].settings_path == (
            home / "Library" / "Application Support" / "Code" / "User" / "settings.json"
        )

    def test_linux_uses_xdg_config_home_or_dot_config(self, monkeypatch, home):
        monkeypatch.setattr(vscode.sys, "platform", "linux")
        assert self._by_name()["VS Code"].settings_path == (
            home / ".config" / "Code" / "User" / "settings.json"
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
        assert (
            self._by_name()["VS Code"].settings_path
            == home / "xdg" / "Code" / "User" / "settings.json"
        )

    def test_vscode_server_uses_machine_settings(self, monkeypatch, home):
        monkeypatch.setattr(vscode.sys, "platform", "linux")
        server = self._by_name()["VS Code Server"]
        assert server.extensions_dir == home / ".vscode-server" / "extensions"
        assert (
            server.settings_path == home / ".vscode-server" / "data" / "Machine" / "settings.json"
        )


class TestDetection:
    @pytest.mark.parametrize(
        ("folder", "expected"),
        [
            ("anthropic.claude-code-2.1.282", True),
            ("anthropic.claude-code-2.1.282-win32-x64", True),
            ("Anthropic.Claude-Code-2.1.282", True),
            ("anthropic.claude-codex-1.0.0", False),
            ("ms-python.python-2026.1.0", False),
            (None, False),
        ],
    )
    def test_extension_folder(self, tmp_path, folder, expected):
        assert vscode.has_claude_extension(_install(tmp_path, extension=folder)) is expected

    def test_missing_extensions_dir(self, tmp_path):
        install = vscode.VSCodeInstall("VS Code", tmp_path / "nope", tmp_path / "settings.json")
        assert vscode.has_claude_extension(install) is False


class TestProfiles:
    CLAUDE = vscode.CLAUDE_EXTENSION_ID

    def _names(self, install):
        return [(t.name, t.settings_path) for t in vscode.claude_settings_targets(install)]

    def test_extensions_json_is_authoritative_over_leftover_folders(self, tmp_path):
        # VS Code keeps old version folders until cleanup; the registry says what's installed.
        install = _install(tmp_path, listed=("ms-python.python",))
        assert self._names(install) == []

    def test_folder_check_when_there_is_no_extensions_json(self, tmp_path):
        install = _install(tmp_path)
        assert self._names(install) == [("VS Code", install.settings_path)]

    def test_each_profile_with_the_extension_gets_its_own_settings(self, tmp_path):
        profiles = [
            {"name": "Dev", "location": "-16c12516"},
            {"name": "QA", "location": "-712ed176"},
            {
                "name": "Agents",
                "location": "builtin/agents",
                "useDefaultFlags": {"settings": True, "extensions": True},
            },
        ]
        install = _install(tmp_path, listed=(self.CLAUDE,), profiles=profiles)
        user = install.user_dir
        _extensions_json(user / "profiles" / "-16c12516" / "extensions.json", self.CLAUDE)
        _extensions_json(user / "profiles" / "-712ed176" / "extensions.json", "ms-python.python")

        assert self._names(install) == [
            ("VS Code", install.settings_path),
            ("VS Code (profile Dev)", user / "profiles" / "-16c12516" / "settings.json"),
            # "Agents" shares the default profile's settings, so it isn't listed twice.
        ]

    def test_profile_sharing_default_extensions_uses_its_own_settings(self, tmp_path):
        profiles = [{"name": "Work", "location": "abc", "useDefaultFlags": {"extensions": True}}]
        install = _install(tmp_path, listed=(self.CLAUDE,), profiles=profiles)

        assert self._names(install)[-1] == (
            "VS Code (profile Work)",
            install.user_dir / "profiles" / "abc" / "settings.json",
        )

    def test_profile_only_install(self, tmp_path):
        # The default profile doesn't have the extension; one named profile does.
        profiles = [{"name": "Dev", "location": "dev"}]
        install = _install(tmp_path, extension=None, listed=(), profiles=profiles)
        _extensions_json(install.user_dir / "profiles" / "dev" / "extensions.json", self.CLAUDE)

        assert self._names(install) == [
            ("VS Code (profile Dev)", install.user_dir / "profiles" / "dev" / "settings.json")
        ]

    def test_rejects_absolute_parent_and_resolved_outside_profile_locations(self, tmp_path):
        profiles = [
            {
                "name": "Absolute",
                "location": str(tmp_path / "outside"),
                "useDefaultFlags": {"extensions": True},
            },
            {"name": "Parent", "location": "../outside", "useDefaultFlags": {"extensions": True}},
            {
                "name": "Nested parent",
                "location": "nested/../safe",
                "useDefaultFlags": {"extensions": True},
            },
            {"name": "Symlink", "location": "link", "useDefaultFlags": {"extensions": True}},
        ]
        install = _install(tmp_path, profiles=profiles)
        outside = tmp_path / "outside"
        outside.mkdir()
        (install.user_dir / "profiles").mkdir(parents=True, exist_ok=True)
        (install.user_dir / "profiles" / "link").symlink_to(outside, target_is_directory=True)

        assert vscode.claude_settings_targets(install) == [
            vscode.SettingsTarget("VS Code", install.settings_path)
        ]

    def test_configure_writes_the_profile_settings(self, tmp_path, wrapper, messages):
        profiles = [{"name": "Dev", "location": "dev"}]
        install = _install(tmp_path, extension=None, listed=(), profiles=profiles)
        _extensions_json(install.user_dir / "profiles" / "dev" / "extensions.json", self.CLAUDE)

        _configure(install)

        profile_settings = install.user_dir / "profiles" / "dev" / "settings.json"
        assert json.loads(profile_settings.read_text(encoding="utf-8"))[WRAPPER_KEY] == WRAPPER
        assert not install.settings_path.exists()
        assert "profile Dev" in messages["success"][0]

    def test_unreadable_profile_list_falls_back_to_the_default_profile(self, tmp_path):
        install = _install(tmp_path, listed=(self.CLAUDE,))
        storage = install.user_dir / "globalStorage" / "storage.json"
        storage.parent.mkdir(parents=True)
        storage.write_text("{not json", encoding="utf-8")

        assert self._names(install) == [("VS Code", install.settings_path)]


class TestConfigure:
    def test_creates_settings_when_absent(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)

        _configure(install)

        assert _settings(install) == {WRAPPER_KEY: WRAPPER, LOGIN_KEY: True, MODE_KEY: "default"}
        assert len(messages["success"]) == 1 and "Reload VS Code" in messages["success"][0]

    def test_keeps_other_settings_and_the_users_permission_mode(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _write_settings(install, {"editor.fontSize": 14, MODE_KEY: "acceptEdits"})

        _configure(install)

        assert _settings(install) == {
            "editor.fontSize": 14,
            MODE_KEY: "acceptEdits",
            WRAPPER_KEY: WRAPPER,
            LOGIN_KEY: True,
        }

    def test_second_run_changes_nothing(self, tmp_path, monkeypatch, wrapper, messages):
        install = _install(tmp_path)
        _configure(install)
        writes = []
        monkeypatch.setattr(config_io, "write_text_file", lambda path, text: writes.append(path))

        _configure(install)

        assert writes == []
        assert messages["note"] == [
            "VS Code: the Claude Code extension already uses Unity Gateway."
        ]

    def test_leaves_settings_with_comments_untouched(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        original = '{\n    // my font\n    "editor.fontSize": 14,\n}\n'
        _write_settings(install, original)

        _configure(install)

        assert install.settings_path.read_text(encoding="utf-8") == original
        assert len(messages["warning"]) == 1
        assert f'"{WRAPPER_KEY}": "{WRAPPER}",' in messages["warning"][0]

    def test_does_not_replace_another_wrapper(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _write_settings(install, {WRAPPER_KEY: "C:/tools/my-wrapper.exe"})

        _configure(install)

        assert _settings(install) == {WRAPPER_KEY: "C:/tools/my-wrapper.exe"}
        assert "my-wrapper.exe" in messages["warning"][0]

    def test_moves_its_own_wrapper_to_the_current_install(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _write_settings(install, {WRAPPER_KEY: "C:/old/bin/ug-claude-vscode.exe", LOGIN_KEY: True})

        _configure(install)

        assert _settings(install)[WRAPPER_KEY] == WRAPPER

    def test_bom_prefixed_settings_are_read(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        install.settings_path.parent.mkdir(parents=True)
        install.settings_path.write_bytes(b'\xef\xbb\xbf{"editor.fontSize": 14}')

        _configure(install)

        assert _settings(install)["editor.fontSize"] == 14
        assert _settings(install)[WRAPPER_KEY] == WRAPPER

    def test_no_extension_is_silent(self, tmp_path, monkeypatch, messages):
        install = _install(tmp_path, extension=None)
        monkeypatch.setattr(vscode, "wrapper_executable", lambda: pytest.fail("not needed"))

        _configure(install)

        assert not install.settings_path.exists()
        assert messages == {"warning": [], "note": [], "success": []}

    def test_missing_wrapper_warns_and_writes_nothing(self, tmp_path, monkeypatch, messages):
        install = _install(tmp_path)
        monkeypatch.setattr(vscode, "wrapper_executable", lambda: None)

        _configure(install)

        assert not install.settings_path.exists()
        assert "reinstall Unity Gateway" in messages["warning"][0]

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch, wrapper, messages):
        install = _install(tmp_path)
        monkeypatch.setattr(config_io, "_dry_run", True)

        _configure(install)

        assert not install.settings_path.exists()
        assert not (config_io.APP_DIR / "vscode-claude-extension.json").exists()

    def test_configures_every_install_with_the_extension(self, tmp_path, wrapper, messages):
        stable = _install(tmp_path, name="VS Code")
        insiders = _install(tmp_path, name="VS Code Insiders")
        without = _install(tmp_path, name="VS Code Server", extension=None)

        vscode.configure_claude_extension(installs=lambda: [stable, insiders, without])

        assert _settings(stable)[WRAPPER_KEY] == WRAPPER
        assert _settings(insiders)[WRAPPER_KEY] == WRAPPER
        assert not without.settings_path.exists()

    def test_persists_the_revert_record_before_settings_write(
        self, tmp_path, monkeypatch, wrapper, messages
    ):
        install = _install(tmp_path)
        events: list[str] = []
        monkeypatch.setattr(vscode, "_write_record", lambda _record: events.append("record"))
        monkeypatch.setattr(
            config_io,
            "write_text_file",
            lambda _path, _text: events.append("settings"),
        )

        _configure(install)

        assert events.index("record") < events.index("settings")

    def test_continues_after_one_profile_write_fails_and_keeps_its_record(
        self, tmp_path, monkeypatch, wrapper, messages
    ):
        stable = _install(tmp_path, name="VS Code")
        insiders = _install(tmp_path, name="VS Code Insiders")
        write_text_file = config_io.write_text_file

        def write(path, text):
            if path == insiders.settings_path:
                raise RuntimeError("read-only")
            write_text_file(path, text)

        monkeypatch.setattr(config_io, "write_text_file", write)

        vscode.configure_claude_extension(installs=lambda: [stable, insiders])

        assert _settings(stable)[WRAPPER_KEY] == WRAPPER
        assert not insiders.settings_path.exists()
        assert "VS Code Insiders" in messages["warning"][0]
        assert set(_record()) == {str(stable.settings_path), str(insiders.settings_path)}


class TestRevert:
    def test_removes_only_what_ug_added(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _write_settings(install, {"editor.fontSize": 14, LOGIN_KEY: False})
        _configure(install)

        assert vscode.revert_claude_extension() == "restored"

        assert _settings(install) == {"editor.fontSize": 14, LOGIN_KEY: False}
        assert not (config_io.APP_DIR / "vscode-claude-extension.json").exists()

    def test_keeps_values_changed_since(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _configure(install)
        doc = _settings(install)
        doc[MODE_KEY] = "plan"
        _write_settings(install, doc)

        vscode.revert_claude_extension()

        assert _settings(install) == {MODE_KEY: "plan"}

    def test_restores_an_older_ug_wrapper_path(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _write_settings(install, {WRAPPER_KEY: "C:/old/bin/ug-claude-vscode.exe"})
        _configure(install)

        vscode.revert_claude_extension()

        assert _settings(install)[WRAPPER_KEY] == "C:/old/bin/ug-claude-vscode.exe"

    def test_retains_a_jsonc_record_and_reports_manual_cleanup(self, tmp_path, wrapper, messages):
        install = _install(tmp_path)
        _configure(install)
        _write_settings(install, '{\n    // user comment\n    "editor.fontSize": 14,\n}\n')

        assert vscode.revert_claude_extension() == "unchanged (manual cleanup needed)"

        assert set(_record()) == {str(install.settings_path)}
        assert "retained its revert record" in messages["warning"][0]

    def test_restores_other_profiles_when_one_revert_write_fails(
        self, tmp_path, monkeypatch, wrapper, messages
    ):
        stable = _install(tmp_path, name="VS Code")
        insiders = _install(tmp_path, name="VS Code Insiders")
        vscode.configure_claude_extension(installs=lambda: [stable, insiders])
        write_text_file = config_io.write_text_file

        def write(path, text):
            if path == insiders.settings_path:
                raise RuntimeError("read-only")
            write_text_file(path, text)

        monkeypatch.setattr(config_io, "write_text_file", write)

        assert vscode.revert_claude_extension() == "partially restored"

        assert _settings(stable) == {}
        assert _settings(insiders)[WRAPPER_KEY] == WRAPPER
        assert set(_record()) == {str(insiders.settings_path)}
        assert "couldn't restore" in messages["warning"][0]

    def test_nothing_recorded(self):
        assert vscode.revert_claude_extension() == "unchanged"


class TestWrapperExecutable:
    def test_prefers_path_when_no_sibling_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.sys, "argv", [str(tmp_path / "ug")])
        monkeypatch.setattr(vscode.shutil, "which", lambda name: f"/bin/{name}")
        assert vscode.wrapper_executable() == "/bin/ug-claude-vscode"

    def test_prefers_sibling_to_path(self, monkeypatch, tmp_path):
        suffix = ".exe" if vscode.os.name == "nt" else ""
        active_ug = tmp_path / f"ug{suffix}"
        sibling = tmp_path / f"ug-claude-vscode{suffix}"
        active_ug.write_text("", encoding="utf-8")
        sibling.write_text("", encoding="utf-8")
        monkeypatch.setattr(vscode.sys, "argv", [str(active_ug)])
        monkeypatch.setattr(vscode.shutil, "which", lambda name: f"/bin/{name}")

        assert Path(vscode.wrapper_executable()) == sibling

    def test_falls_back_to_next_to_ug(self, monkeypatch, tmp_path):
        suffix = ".exe" if vscode.os.name == "nt" else ""
        (tmp_path / f"ug-claude-vscode{suffix}").write_text("", encoding="utf-8")
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode.sys, "argv", [str(tmp_path / f"ug{suffix}")])

        assert Path(vscode.wrapper_executable()) == tmp_path / f"ug-claude-vscode{suffix}"

    def test_not_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode.sys, "argv", [str(tmp_path / "ug")])

        assert vscode.wrapper_executable() is None
