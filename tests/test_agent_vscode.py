"""Tests for `ug configure --agents vscode` and its revert."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ucode import config_io
from ucode.agents import vscode
from ucode.cli import app
from ucode.managed_files import OS

# The conftest fixture stubs these out for every test; the ones here exercise the real lookups.
REAL_FIND_CODE = vscode.find_code
REAL_SETTINGS_PATH = vscode.settings_path

WS = "https://ws.cloud.databricks.com"
STATE = {"workspace": WS, "profile": "dev"}
USER_SETTINGS = """// My settings
{
    // Theme
    "workbench.colorTheme": "Default Dark+",
    "editor.fontSize": 14, // big
}
"""


class FakeCode:
    """Stands in for VS Code's `code` command: records calls and tracks the installed version."""

    def __init__(self, installed: str | None = None, fail: str | None = None) -> None:
        self.installed = installed
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []
        self.vsix_entries: list[str] = []

    def __call__(self, code: Path, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[0] == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if args[0] == "--list-extensions":
            listed = f"{vscode.EXTENSION_ID}@{self.installed}\n" if self.installed else ""
            return subprocess.CompletedProcess(args, 0, "ms-python.python@2026.1.0\n" + listed, "")
        if args[0] == "--install-extension":
            self.vsix_entries = zipfile.ZipFile(args[1]).namelist()
            self.installed = json.loads((vscode.EXTENSION_DIR / "package.json").read_text())[
                "version"
            ]
        if args[0] == "--uninstall-extension":
            self.installed = None
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def code(monkeypatch):
    fake = FakeCode()
    monkeypatch.setattr(vscode, "find_code", lambda: (Path("/fake/bin/code"), "Code"))
    monkeypatch.setattr(vscode, "_run_code", fake)
    monkeypatch.setattr(vscode, "launch_token", lambda state, workspace: "token")
    monkeypatch.setattr(
        vscode,
        "discover_model_services",
        lambda workspace, token: (
            {"sonnet": "system.ai.claude-sonnet-5-5"},
            ["system.ai.gpt-6-luna", "system.ai.gpt-oss-120b"],
            ["system.ai.gemini-4-argon"],
            ["system.ai.gpt-oss-120b", "system.ai.deepseek-v4-1-flash"],
            None,
        ),
    )
    return fake


def _printed(capsys) -> str:
    return " ".join(capsys.readouterr().out.split())


def _settings() -> Path:
    return vscode.settings_path("Code")


def _settings_values() -> dict:
    return config_io.loads_jsonc(_settings().read_text(encoding="utf-8"))


class TestConfigure:
    def test_merges_settings_keeping_the_users_own(self, code):
        _settings().parent.mkdir(parents=True)
        _settings().write_text(USER_SETTINGS, encoding="utf-8")

        assert vscode.configure(STATE)

        text = _settings().read_text(encoding="utf-8")
        for kept in ("// My settings", "// Theme", '"editor.fontSize": 14, // big'):
            assert kept in text
        assert _settings_values() == {
            "workbench.colorTheme": "Default Dark+",
            "editor.fontSize": 14,
            "unityGateway.workspace": WS,
            "unityGateway.models": [
                {
                    "id": "system.ai.claude-sonnet-5-5",
                    "api": "chat_completions",
                    "maxInputTokens": 872_000,
                    "maxOutputTokens": 128_000,
                },
                {"id": "system.ai.gpt-6-luna", "api": "responses"},
                {"id": "system.ai.gpt-oss-120b", "api": "chat_completions"},
                {"id": "system.ai.deepseek-v4-1-flash", "api": "chat_completions"},
            ],
            "unityGateway.authCommand": vscode.build_auth_shell_command(WS, "dev"),
        }

    def test_creates_missing_settings_and_installs_the_packed_extension(self, code):
        assert vscode.configure(STATE)

        assert set(_settings_values()) == set(vscode.SETTINGS)
        assert code.calls[-1][0] == "--install-extension"
        assert code.calls[-1][2] == "--force"
        assert code.vsix_entries == [
            "[Content_Types].xml",
            "extension.vsixmanifest",
            "extension/media/databricks.svg",
            "extension/out/extension.js",
            "extension/package.json",
        ]

    def test_reruns_update_values_and_reinstall_the_same_version(self, code):
        vscode.configure(STATE)
        code.calls.clear()

        assert vscode.configure({**STATE, "profile": "other"})

        assert "--profile other" in _settings_values()["unityGateway.authCommand"]
        assert [call[0] for call in code.calls] == ["--install-extension"]

    def test_leaves_unreadable_settings_alone(self, code, capsys):
        _settings().parent.mkdir(parents=True)
        _settings().write_text('{"broken": ', encoding="utf-8")

        assert not vscode.configure(STATE)

        assert _settings().read_text(encoding="utf-8") == '{"broken": '
        assert code.calls == []
        assert "Couldn't update" in _printed(capsys)

    def test_warns_when_vscode_isnt_found(self, capsys):
        assert not vscode.configure(STATE)

        assert "`code` command wasn't found" in _printed(capsys)
        assert not _settings().exists()

    def test_reports_a_failed_install(self, code, capsys):
        code.fail = "--install-extension"

        assert not vscode.configure(STATE)

        assert "--install-extension` failed: boom" in _printed(capsys)


class TestRevert:
    def test_removes_only_ugs_settings_and_uninstalls(self, code):
        _settings().parent.mkdir(parents=True)
        _settings().write_text(USER_SETTINGS, encoding="utf-8")
        vscode.configure(STATE)

        assert vscode.revert() == "removed"

        assert _settings_values() == {
            "workbench.colorTheme": "Default Dark+",
            "editor.fontSize": 14,
        }
        assert "// Theme" in _settings().read_text(encoding="utf-8")
        assert code.calls[-1] == ("--uninstall-extension", vscode.EXTENSION_ID)

    def test_does_nothing_when_vscode_was_never_configured(self, code):
        _settings().parent.mkdir(parents=True)
        _settings().write_text(USER_SETTINGS, encoding="utf-8")

        assert vscode.revert() == "unchanged"

        assert _settings().read_text(encoding="utf-8") == USER_SETTINGS
        assert [call[0] for call in code.calls] == ["--list-extensions"]


class TestFindCode:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch):
        monkeypatch.setattr(vscode, "find_code", REAL_FIND_CODE)

    def test_prefers_code_on_path(self, monkeypatch):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: f"/usr/local/bin/{name}")

        assert vscode.find_code() == (Path("/usr/local/bin/code"), "Code")

    def test_falls_back_to_insiders(self, monkeypatch):
        monkeypatch.setattr(
            vscode.shutil,
            "which",
            lambda name: "/opt/bin/code-insiders" if name == "code-insiders" else None,
        )

        assert vscode.find_code() == (Path("/opt/bin/code-insiders"), "Code - Insiders")

    def test_finds_the_macos_app_when_code_isnt_on_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        monkeypatch.setattr(vscode.Path, "home", lambda: tmp_path)
        app_code = tmp_path / "Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
        app_code.parent.mkdir(parents=True)
        app_code.touch()

        assert vscode.find_code() == (app_code, "Code")

    def test_finds_the_windows_user_install(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode, "current_os", lambda: OS.WINDOWS)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        code_cmd = tmp_path / "Programs/Microsoft VS Code/bin/code.cmd"
        code_cmd.parent.mkdir(parents=True)
        code_cmd.touch()

        assert vscode.find_code() == (code_cmd, "Code")

    def test_none_when_vscode_isnt_installed(self, monkeypatch):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode, "_install_locations", lambda command: [])

        assert vscode.find_code() is None


class TestSettingsPath:
    @pytest.mark.parametrize(
        ("system", "env", "expected"),
        [
            (OS.LINUX, {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/Code/User/settings.json"),
            (OS.WINDOWS, {"APPDATA": "/appdata"}, "/appdata/Code/User/settings.json"),
        ],
    )
    def test_user_settings_per_os(self, monkeypatch, system, env, expected):
        monkeypatch.setattr(vscode, "current_os", lambda: system)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        assert REAL_SETTINGS_PATH("Code") == Path(expected)

    def test_macos(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        monkeypatch.setattr(vscode.Path, "home", lambda: tmp_path)

        assert (
            REAL_SETTINGS_PATH("Code - Insiders")
            == tmp_path / "Library/Application Support/Code - Insiders/User/settings.json"
        )


class TestAuthCommand:
    def test_pat_and_custom_oauth(self):
        assert "--use-pat" in vscode.auth_command({**STATE, "use_pat": True})
        custom = {
            "client_id": "c",
            "redirect_url": "http://localhost:8020",
            "scopes": "all-apis",
            "profile": "custom",
        }
        assert "--profile custom" in vscode.auth_command({"workspace": WS, "custom_oauth": custom})


def test_configure_agents_vscode_sets_up_the_workspace_then_vscode(monkeypatch):
    configured: list[dict] = []
    shared: list[dict] = []
    monkeypatch.setattr(
        "ucode.cli._configure_shared_workspace_states",
        lambda *args, **kwargs: shared.append(kwargs) or [],
    )
    monkeypatch.setattr(
        "ucode.cli.configure_workspace_command",
        lambda **kwargs: pytest.fail("no model agents to configure"),
    )
    monkeypatch.setattr("ucode.cli.load_state", lambda: STATE)
    monkeypatch.setattr(vscode, "configure", lambda state: configured.append(state) or True)

    result = CliRunner().invoke(app, ["configure", "--agents", "vscode", "--workspace", WS])

    assert result.exit_code == 0, result.output
    assert shared[0]["tools"] == []
    assert configured == [STATE]
