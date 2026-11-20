"""Tests for Copilot Chat in VS Code: `ug configure --agents copilot` sets it up, and revert."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ucode.agents as agents_mod
import ucode.cli as cli_mod
from ucode import config_io
from ucode import databricks as databricks_module
from ucode.agents import copilot, vscode
from ucode.cli import app
from ucode.databricks import AnthropicModelCatalog
from ucode.managed_files import OS

# The conftest fixture stubs these out for every test; the ones here exercise the real lookups.
REAL_FIND_CODE = vscode.find_code
REAL_FIND_COMMAND = vscode._find_command
REAL_FIND_INSTALL = vscode.find_install
REAL_FIRST_CODE_ON_PATH = vscode.first_code_on_path
REAL_INSTALL_LOCATIONS = vscode._install_locations
REAL_RUN_CODE = vscode._run_code
REAL_USER_DIR = vscode.user_dir

WS = "https://ws.cloud.databricks.com"
# What `ug configure` saved for the Copilot CLI, which VS Code reuses.
DISCOVERED = [
    copilot.CopilotModel("system.ai.claude-sonnet-5-5", "anthropic"),
    copilot.CopilotModel("system.ai.gpt-6-luna", "responses", (922_000, 128_000)),
]
STATE = {"workspace": WS, "profile": "dev", "copilot_catalog": copilot.catalog_state(DISCOVERED)}
VERSION = json.loads((vscode.EXTENSION_DIR / "package.json").read_text())["version"]


class FakeCode:
    """Stands in for VS Code's `code` command: records calls and what an install unpacks."""

    def __init__(self, extensions: Path) -> None:
        self.extensions = extensions
        self.fail: str | None = None
        self.calls: list[tuple[str, ...]] = []
        self.paths: list[Path] = []
        self.vsix_entries: list[str] = []
        self.config: dict | None = None

    @property
    def folder(self) -> Path:
        return self.extensions / f"{vscode.EXTENSION_ID}-{VERSION}"

    def __call__(self, code: Path, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        self.paths.append(code)
        if args[0] == self.fail:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        if args[0] == "--list-extensions":
            listed = f"{vscode.EXTENSION_ID}@{VERSION}\n" if self.folder.exists() else ""
            return subprocess.CompletedProcess(args, 0, "ms-python.python@2026.1.0\n" + listed, "")
        if args[0] == "--install-extension":
            with zipfile.ZipFile(args[1]) as vsix:
                self.vsix_entries = vsix.namelist()
                self.config = json.loads(vsix.read(f"extension/{vscode.CONFIG_FILE}"))
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / vscode.CONFIG_FILE).write_text(json.dumps(self.config))
        if args[0] == "--uninstall-extension":
            shutil.rmtree(self.folder)
        return subprocess.CompletedProcess(args, 0, "", "")


FAKE_INSTALL = vscode.VSCodeInstall(
    Path("/fake/bin/code"), Path("/fake/app"), {"nameShort": "Code", "version": "1.147.0"}
)


@pytest.fixture
def code(monkeypatch):
    fake = FakeCode(vscode.extensions_dir(".vscode"))
    monkeypatch.setattr(vscode, "find_code", lambda: Path("/fake/bin/code"))
    monkeypatch.setattr(vscode, "find_install", lambda prefer_runtime=False: FAKE_INSTALL)
    monkeypatch.setattr(vscode, "first_code_on_path", lambda: Path("/fake/bin/code"))
    monkeypatch.setattr(vscode, "_find_command", lambda command: Path(f"/fake/bin/{command}"))
    monkeypatch.setattr(vscode, "_run_code", fake)
    monkeypatch.setattr(copilot, "launch_token", lambda state, workspace: "token")
    return fake


def _printed(capsys) -> str:
    return " ".join(capsys.readouterr().out.split())


class TestConfigure:
    def test_installs_the_extension_with_the_copilot_clis_models_and_limits(self, code, capsys):
        assert vscode.configure(STATE)

        assert code.calls[-1][0] == "--install-extension"
        assert code.calls[-1][2] == "--force"
        assert code.vsix_entries == [
            "[Content_Types].xml",
            "extension.vsixmanifest",
            "extension/media/databricks.svg",
            "extension/out/extension.js",
            "extension/package.json",
            "extension/ug-config.json",
        ]
        assert code.config == {
            "workspace": WS,
            "authCommand": vscode.build_auth_shell_command(WS, "dev"),
            "models": [
                {
                    "id": "system.ai.claude-sonnet-5-5",
                    "name": "Claude Sonnet 5.5",
                    "family": "claude-sonnet-5-5",
                    "api": "anthropic",
                    "maxInputTokens": 872_000,
                    "maxOutputTokens": 128_000,
                },
                {
                    "id": "system.ai.gpt-6-luna",
                    "name": "GPT 6 Luna",
                    "family": "gpt-6-luna",
                    "api": "responses",
                    "maxInputTokens": 794_000,
                    "maxOutputTokens": 128_000,
                },
            ],
        }
        printed = _printed(capsys)
        assert (
            f"VS Code extension: installed {vscode.EXTENSION_ID} {VERSION} with 2 models" in printed
        )
        assert "In VS Code: " in printed

    def test_models_with_the_same_label_show_their_ids(self, code):
        discovered = copilot.build_copilot_catalog(
            ["system.ai.gpt-6-luna", "main.models.gpt-6-luna", "system.ai.gpt-5-5"]
        )
        state = {**STATE, "copilot_catalog": copilot.catalog_state(discovered)}

        models = vscode.model_entries(state)

        assert [model["name"] for model in models] == [
            "main.models.gpt-6-luna",
            "system.ai.gpt-6-luna",
            "GPT 5.5",
        ]

    def test_open_models_use_chat_completions_with_their_limits(self, code):
        discovered = copilot.build_copilot_catalog(
            ["system.ai.kimi-k3-1-pro", "system.ai.deepseek-v4-1-flash-1017", "system.ai.gpt-5-5"]
        )
        state = {**STATE, "copilot_catalog": copilot.catalog_state(discovered)}

        models = vscode.model_entries(state)

        assert models[1:] == [
            {
                "id": "system.ai.deepseek-v4-1-flash-1017",
                "name": "DeepSeek V4.1 Flash 1017",
                "family": "deepseek-v4-1-flash-1017",
                "api": "completions",
                "maxInputTokens": 687_000,
                "maxOutputTokens": 32_000,
            },
            {
                "id": "system.ai.kimi-k3-1-pro",
                "name": "Kimi K3.1 Pro",
                "family": "kimi-k3-1-pro",
                "api": "completions",
                "maxInputTokens": 687_000,
                "maxOutputTokens": 32_000,
            },
        ]

    def test_a_provider_services_claude_targets_keep_their_ids_and_their_limits(
        self, code, monkeypatch, capsys
    ):
        service = {
            "name": "main.default.bedrock",
            "provider_type": "amazon_bedrock",
            "targets": [
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "global.anthropic.claude-opus-4-8",
                "openai.gpt-oss-120b-1:0",
            ],
        }
        monkeypatch.setattr(
            databricks_module, "list_model_provider_services", lambda *a, **k: ([service], None)
        )

        assert vscode.configure(STATE, provider="main.default.bedrock")

        assert code.config["provider"] == "main.default.bedrock"
        assert code.config["models"] == [
            {
                "id": "global.anthropic.claude-opus-4-8",
                "name": "global.anthropic.claude-opus-4-8",
                "family": "claude-opus-4-8",
                "api": "anthropic",
                "maxInputTokens": 872_000,
                "maxOutputTokens": 128_000,
            },
            {
                "id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "name": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "family": "claude-sonnet-4-5",
                "api": "anthropic",
                "maxInputTokens": 168_000,
                "maxOutputTokens": 32_000,
            },
        ]
        printed = _printed(capsys)
        assert "with 2 models from main.default.bedrock" in printed
        assert "that VS Code can't use: openai.gpt-oss-120b-1:0" in printed

    def test_a_openai_provider_services_models_on_responses_and_chat_completions(
        self, code, monkeypatch
    ):
        service = {
            "name": "main.default.openai",
            "provider_type": "openai",
            "allow_all_targets": True,
            "targets": [],
        }
        monkeypatch.setattr(
            databricks_module, "list_model_provider_services", lambda *a, **k: ([service], None)
        )
        monkeypatch.setattr(
            copilot,
            "list_provider_service_models",
            lambda *args: (["gpt-5.5", "gpt-4.1"], None),
        )

        assert vscode.configure(STATE, provider="main.default.openai")

        assert code.config["models"] == [
            {"id": "gpt-5.5", "name": "gpt-5.5", "family": "gpt-5.5", "api": "responses"},
            {"id": "gpt-4.1", "name": "gpt-4.1", "family": "gpt-4.1", "api": "completions"},
        ]

    @pytest.mark.parametrize(
        ("service", "error"),
        [
            ({"provider_type": "gemini_enterprise"}, "which vscode can't route to"),
            ({"provider_type": "anthropic", "relayed": True}, "only Claude Code can use"),
            (
                {"provider_type": "openai", "allow_all_targets": True},
                "Could not list the models of 'main.default.mps': forbidden.",
            ),
        ],
    )
    def test_keeps_the_installed_setup_for_a_provider_service_it_cant_use(
        self, code, monkeypatch, capsys, service, error
    ):
        service = {"name": "main.default.mps", "targets": [], **service}
        monkeypatch.setattr(
            databricks_module, "list_model_provider_services", lambda *a, **k: ([service], None)
        )
        monkeypatch.setattr(
            copilot, "list_provider_service_models", lambda *args: ([], "forbidden")
        )

        assert not vscode.configure(STATE, provider="main.default.mps")

        assert code.calls == []
        printed = _printed(capsys)
        assert error in printed
        assert "VS Code was left unchanged." in printed

    def test_a_model_locations_services_with_their_listed_limits(self, code, monkeypatch):
        listed: list[str | None] = []

        def listing(workspace, token, *, parent_schema=None):
            listed.append(parent_schema)
            return AnthropicModelCatalog(
                model_ids=["main.models.claude-opus-5-5", "main.models.team-assistant"],
                model_id_to_display_name={},
                model_id_to_limits={"main.models.claude-opus-5-5": (200_000, 64_000)},
            )

        monkeypatch.setattr(copilot, "list_anthropic_model_catalog", listing)

        assert vscode.configure(STATE, location="main.models")

        assert listed == ["main.models"]
        assert "provider" not in code.config
        assert code.config["models"] == [
            {
                "id": "main.models.claude-opus-5-5",
                "name": "Claude Opus 5.5",
                "family": "claude-opus-5-5",
                "api": "anthropic",
                "maxInputTokens": 136_000,
                "maxOutputTokens": 64_000,
            },
            {
                "id": "main.models.team-assistant",
                "name": "Team Assistant",
                "family": "team-assistant",
                "api": "anthropic",
            },
        ]

    def test_reruns_reinstall_with_the_new_setup(self, code):
        vscode.configure(STATE)
        code.calls.clear()

        assert vscode.configure({**STATE, "profile": "other"})

        assert "--profile other" in code.config["authCommand"]
        assert [call[0] for call in code.calls] == ["--install-extension"]

    def test_warns_when_vscode_isnt_found(self, capsys):
        assert not vscode.configure(STATE)

        assert "`code` command wasn't found" in _printed(capsys)

    @pytest.mark.parametrize("reason", ["the listing failed", None])
    def test_keeps_the_installed_setup_when_no_models_are_found(
        self, code, monkeypatch, capsys, reason
    ):
        vscode.configure(STATE)
        installed = code.config
        code.calls.clear()
        state = {
            "workspace": "https://other.cloud.databricks.com",
            "copilot_catalog": copilot.catalog_state([]),
            "_discovery_reasons": {"copilot": reason} if reason else {},
        }

        assert not vscode.configure(state)

        assert code.calls == []
        assert code.config == installed
        assert (
            "No Claude, GPT, GLM, Kimi or DeepSeek models for VS Code "
            f"({reason or 'the workspace has none'})"
        ) in (_printed(capsys))

    def test_reports_a_code_command_that_cant_run(self, code, monkeypatch, capsys):
        def hang(*args, **kwargs):
            raise subprocess.TimeoutExpired("code", 120)

        monkeypatch.setattr(vscode, "_run_code", REAL_RUN_CODE)
        monkeypatch.setattr(vscode.subprocess_cross_os, "run", hang)

        assert not vscode.configure(STATE)

        assert f"`{Path('/fake/bin/code')} --install-extension` failed" in _printed(capsys)

    def test_reports_a_failed_install(self, code, capsys):
        code.fail = "--install-extension"

        assert not vscode.configure(STATE)

        assert "--install-extension` failed: boom" in _printed(capsys)


UG_EXE = r"C:\uv\bin\ug.exe"


class TestSeatRelay:
    SEATS = "main.ai.copilot_seats"
    COMMAND = ["/bin/ug", "copilot-seat-runtime", "--provider", SEATS, "--host", WS]

    @pytest.fixture
    def runtime(self, tmp_path):
        path = tmp_path / "vscode-app" / "copilot-runtime"
        path.parent.mkdir()
        path.write_text("")
        return path

    @pytest.fixture
    def seats(self, monkeypatch, runtime):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.LINUX)
        monkeypatch.setattr(vscode, "uses_seat_relay", lambda state, provider, tool: True)
        monkeypatch.setattr(vscode, "ug_binary", lambda: "/bin/ug")
        monkeypatch.setattr(vscode, "bundled_runtime", lambda code: runtime)

    @staticmethod
    def _settings() -> Path:
        return vscode.user_dir(insiders=False) / "settings.json"

    @staticmethod
    def _wrapper() -> Path:
        return Path(json.loads(TestSeatRelay._settings().read_text())[vscode.RUNTIME_PATH_SETTING])

    @staticmethod
    def _wrappers() -> list[Path]:
        return list(vscode._wrapper_dir(insiders=False).glob("ug-copilot-runtime*"))

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_routes_copilot_through_the_seat_relay_without_a_restart(
        self, code, seats, runtime, capsys
    ):
        assert vscode.configure(STATE, provider=self.SEATS)

        assert code.config == {
            "workspace": WS,
            "authCommand": vscode.build_auth_shell_command(WS, "dev"),
            "models": [],
            "provider": self.SEATS,
        }
        settings = json.loads(self._settings().read_text())
        assert settings == {vscode.RUNTIME_PATH_SETTING: str(self._wrapper())}
        script = self._wrapper().read_text()
        assert script.startswith("#!/bin/sh\nexec /bin/ug copilot-seat-runtime --provider ")
        assert f'--profile dev --runtime {runtime} --code /fake/bin/code -- "$@"' in script
        assert self._wrapper().stat().st_mode & 0o777 == 0o700
        printed = _printed(capsys)
        assert f"routing Copilot through {self.SEATS}" in printed
        assert "sign in to GitHub Copilot, then start a chat in a Copilot session" in printed
        assert "quit VS Code" not in printed

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_keeps_the_developers_settings_and_comments(self, code, seats):
        self._settings().parent.mkdir(parents=True)
        self._settings().write_text('{\n    // mine\n    "editor.tabSize": 2,\n}\n')

        assert vscode.configure(STATE, provider=self.SEATS)

        (wrapper,) = self._wrappers()
        assert self._settings().read_text() == (
            '{\n    // mine\n    "editor.tabSize": 2,\n'
            f'    "{vscode.RUNTIME_PATH_SETTING}": "{wrapper}",\n}}\n'
        )

    def test_without_a_bundled_runtime_leaves_vscode_unchanged(
        self, code, seats, monkeypatch, capsys
    ):
        monkeypatch.setattr(vscode, "bundled_runtime", lambda code: None)

        assert not vscode.configure(STATE, provider=self.SEATS)

        assert code.config is None
        assert not self._settings().exists()
        assert "no bundled Copilot runtime" in _printed(capsys)

    def test_a_settings_file_ug_cant_edit_is_reported(self, code, seats, capsys):
        self._settings().parent.mkdir(parents=True)
        self._settings().write_text("[]")

        assert not vscode.configure(STATE, provider=self.SEATS)

        printed = _printed(capsys)
        assert f"GitHub Copilot in VS Code doesn't go through {self.SEATS}" in printed
        assert vscode.RUNTIME_PATH_SETTING in printed
        assert self._settings().read_text() == "[]"

    def test_a_failed_seat_relay_check_leaves_vscode_unchanged(self, code, monkeypatch, capsys):
        def uses_seat_relay(state, provider, tool):
            raise RuntimeError("DATABRICKS_BEARER_COMMAND failed.")

        monkeypatch.setattr(vscode, "uses_seat_relay", uses_seat_relay)

        assert not vscode.configure(STATE, provider=self.SEATS)

        assert code.config is None
        assert "VS Code was left unchanged" in _printed(capsys)

    def test_windows_routes_copilot_through_a_node_wrapper(
        self, code, seats, runtime, monkeypatch, capsys
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode, "ug_binary", lambda: UG_EXE)

        assert vscode.configure(STATE, provider=self.SEATS)

        wrapper = self._wrapper()
        assert wrapper.parent == vscode._wrapper_dir(insiders=False)
        assert wrapper.name.startswith("ug-copilot-runtime-")
        assert wrapper.suffix == ".js"
        script = wrapper.read_text()
        assert (
            'spawn("C:\\\\uv\\\\bin\\\\ug.exe", [...["copilot-seat-runtime", "--provider", '
            in script
        )
        assert (
            f'"--profile", "dev", "--runtime", {json.dumps(str(runtime))}, '
            f'"--code", {json.dumps(str(Path("/fake/bin/code")))}, "--"]' in script
        )
        assert "...process.argv.slice(2)" in script
        printed = _printed(capsys)
        assert f"routing Copilot through {self.SEATS}" in printed
        assert "can't route" not in printed

    @pytest.mark.parametrize("found", [r"C:\tools\ug.cmd", "ug"])
    def test_windows_uses_the_running_ug_exe_over_a_shim_node_cant_start(
        self, code, seats, monkeypatch, found
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode, "ug_binary", lambda: found)
        monkeypatch.setattr(vscode, "running_launcher", lambda: UG_EXE)

        assert vscode.configure(STATE, provider=self.SEATS)

        assert f"spawn({json.dumps(UG_EXE)}, " in self._wrapper().read_text()

    def test_windows_without_a_ug_exe_leaves_vscode_unchanged(
        self, code, seats, monkeypatch, capsys
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode, "ug_binary", lambda: "ug")
        monkeypatch.setattr(vscode, "running_launcher", lambda: None)

        assert not vscode.configure(STATE, provider=self.SEATS)

        assert code.config is None
        assert not self._settings().exists()
        assert "`ug.exe` wasn't found" in _printed(capsys)

    def test_another_source_removes_the_route(self, code, seats, monkeypatch):
        assert vscode.configure(STATE, provider=self.SEATS)
        monkeypatch.setattr(vscode, "uses_seat_relay", lambda state, provider, tool: False)

        assert vscode.configure(STATE)

        assert json.loads(self._settings().read_text()) == {}
        assert not self._wrappers()

    def test_revert_removes_the_route_but_not_a_developers_own_runtime(self, code, seats):
        assert vscode.configure(STATE, provider=self.SEATS)

        assert vscode.revert() == "removed"

        assert json.loads(self._settings().read_text()) == {}
        assert not self._wrappers()

        self._settings().write_text(json.dumps({vscode.RUNTIME_PATH_SETTING: "/opt/my/runtime"}))
        vscode.revert()
        assert json.loads(self._settings().read_text()) == {
            vscode.RUNTIME_PATH_SETTING: "/opt/my/runtime"
        }

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_a_developers_own_runtime_path_is_left_alone(self, code, seats):
        self._settings().parent.mkdir(parents=True)
        self._settings().write_text(json.dumps({vscode.RUNTIME_PATH_SETTING: "/opt/my/runtime"}))

        assert not vscode.configure(STATE, provider=self.SEATS)

        assert json.loads(self._settings().read_text()) == {
            vscode.RUNTIME_PATH_SETTING: "/opt/my/runtime"
        }
        assert not self._wrappers()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_an_earlier_builds_unhashed_wrapper_is_replaced_and_removed(self, code, seats, capsys):
        old = vscode._wrapper_dir(insiders=False) / vscode.RUNTIME_WRAPPER_NAME
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\n")
        self._settings().parent.mkdir(parents=True)
        self._settings().write_text(json.dumps({vscode.RUNTIME_PATH_SETTING: str(old)}))

        assert vscode.configure(STATE, provider=self.SEATS)

        assert "doesn't replace" not in _printed(capsys)
        assert not old.exists()
        assert self._wrapper().name.startswith(f"{vscode.RUNTIME_WRAPPER_NAME}-")
        assert self._wrappers() == [self._wrapper()]

        self._settings().write_text(json.dumps({vscode.RUNTIME_PATH_SETTING: str(old)}))
        old.write_text("#!/bin/sh\n")
        assert vscode.revert() == "removed"
        assert json.loads(self._settings().read_text()) == {}
        assert not old.exists()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_a_changed_route_gets_a_new_wrapper_so_vscode_restarts_the_runtime(self, code, seats):
        assert vscode.configure(STATE, provider=self.SEATS)
        (first,) = self._wrappers()

        assert vscode.configure({**STATE, "profile": "other"}, provider=self.SEATS)

        (second,) = self._wrappers()
        assert second != first
        assert json.loads(self._settings().read_text()) == {
            vscode.RUNTIME_PATH_SETTING: str(second)
        }

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_a_symlinked_settings_file_stays_a_link_with_its_permissions(
        self, code, seats, tmp_path
    ):
        target = tmp_path / "dotfiles" / "settings.json"
        target.parent.mkdir()
        target.write_text('{\n    "editor.tabSize": 2\n}\n')
        target.chmod(0o600)
        self._settings().parent.mkdir(parents=True)
        self._settings().symlink_to(target)

        assert vscode.configure(STATE, provider=self.SEATS)

        assert self._settings().is_symlink()
        assert target.stat().st_mode & 0o777 == 0o600
        assert json.loads(target.read_text())[vscode.RUNTIME_PATH_SETTING] == str(
            self._wrappers()[0]
        )

    @pytest.mark.skipif(
        sys.platform == "win32", reason="the seat relay wrapper is a POSIX shell script"
    )
    def test_revert_keeps_the_wrapper_when_the_settings_cant_be_edited(self, code, seats):
        assert vscode.configure(STATE, provider=self.SEATS)
        self._settings().write_text("[]")

        vscode.revert()

        assert self._wrappers()


class TestBundledRuntime:
    @pytest.mark.parametrize(
        ("system", "machine", "layout", "target"),
        [
            (
                "MACOS",
                "arm64",
                "Visual Studio Code.app/Contents/Resources/app",
                "darwin-arm64",
            ),
            ("MACOS", "x86_64", "Visual Studio Code.app/Contents/Resources/app", "darwin-x64"),
            ("LINUX", "x86_64", "code/resources/app", "linux-x64"),
            ("LINUX", "aarch64", "code/resources/app", "linux-arm64"),
        ],
    )
    def test_finds_the_runtime_next_to_the_code_command(
        self, tmp_path, monkeypatch, system, machine, layout, target
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: getattr(vscode.OS, system))
        monkeypatch.setattr(vscode.platform, "machine", lambda: machine)
        app = tmp_path / layout
        runtime = (
            app
            / "node_modules.asar.unpacked/@github"
            / f"copilot-sdk-{target}/prebuilds/{target}/copilot-runtime"
        )
        runtime.parent.mkdir(parents=True)
        runtime.write_text("")
        # Linux packages put `code` in <install>/bin; macOS in the app folder's bin.
        bin_dir = app / "bin" if system == "MACOS" else tmp_path / "code" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "code").write_text("")
        link = tmp_path / "on-path" / "code"
        link.parent.mkdir()
        link.symlink_to(bin_dir / "code")

        assert vscode.bundled_runtime(link) == runtime

    def test_no_runtime_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.LINUX)
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "code").write_text("")

        assert vscode.bundled_runtime(tmp_path / "bin" / "code") is None

    @staticmethod
    def _windows_install(root: Path, version: str, target: str) -> Path:
        runtime = (
            root
            / version
            / "resources/app/node_modules.asar.unpacked/@github"
            / f"copilot-sdk-{target}/prebuilds/{target}/copilot-runtime.exe"
        )
        runtime.parent.mkdir(parents=True)
        runtime.write_text("")
        return runtime

    @staticmethod
    def _code_cmd(root: Path, version: str, name: str = "code") -> Path:
        (root / "bin").mkdir(parents=True, exist_ok=True)
        cmd = root / "bin" / f"{name}.cmd"
        cmd.write_text(
            "@echo off\r\nsetlocal\r\nset VSCODE_DEV=\r\nset ELECTRON_RUN_AS_NODE=1\r\n"
            f'"%~dp0..\\Code.exe" "%~dp0..\\{version}\\resources\\app\\out\\cli.js" %*\r\n'
            "endlocal\r\n"
        )
        return cmd

    @pytest.mark.parametrize(
        ("machine", "target"), [("AMD64", "win32-x64"), ("ARM64", "win32-arm64")]
    )
    def test_windows_finds_the_runtime_of_the_version_code_cmd_runs(
        self, tmp_path, monkeypatch, machine, target
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode.platform, "machine", lambda: machine)
        # An update can leave the previous version's folder behind.
        self._windows_install(tmp_path, "0ld0ld0ld0", target)
        runtime = self._windows_install(tmp_path, "8a14f0cc1b", target)
        cmd = self._code_cmd(tmp_path, "8a14f0cc1b")

        assert vscode.bundled_runtime(cmd) == runtime
        # Git Bash finds the extensionless `code` script next to code.cmd.
        (tmp_path / "bin" / "code").write_text("")
        assert vscode.bundled_runtime(tmp_path / "bin" / "code") == runtime

    def test_windows_insiders_reads_its_own_cmd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode.platform, "machine", lambda: "AMD64")
        runtime = self._windows_install(tmp_path, "1234567890", "win32-x64")
        cmd = self._code_cmd(tmp_path, "1234567890", name="code-insiders")

        assert vscode.bundled_runtime(cmd) == runtime

    def test_windows_without_a_version_folder_uses_resources_app(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode.platform, "machine", lambda: "AMD64")
        runtime = self._windows_install(tmp_path, ".", "win32-x64")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "code.cmd").write_text("@echo off\r\n")

        assert vscode.bundled_runtime(tmp_path / "bin" / "code.cmd") == runtime

    def test_windows_without_the_runtime_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode.platform, "machine", lambda: "AMD64")

        assert vscode.bundled_runtime(self._code_cmd(tmp_path, "8a14f0cc1b")) is None

    def test_each_start_runs_the_runtime_of_the_version_vscode_now_has(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vscode, "current_os", lambda: vscode.OS.WINDOWS)
        monkeypatch.setattr(vscode.platform, "machine", lambda: "AMD64")
        configured = self._windows_install(tmp_path, "0ld0ld0ld0", "win32-x64")
        cmd = self._code_cmd(tmp_path, "0ld0ld0ld0")
        assert vscode.resolve_runtime(str(configured), str(cmd)) == str(configured)

        # A VS Code update: code.cmd names the new version folder, the old one may be gone.
        updated = self._windows_install(tmp_path, "n3wn3wn3w0", "win32-x64")
        self._code_cmd(tmp_path, "n3wn3wn3w0")
        shutil.rmtree(tmp_path / "0ld0ld0ld0")
        assert vscode.resolve_runtime(str(configured), str(cmd)) == str(updated)

        # Without a runtime to find, the recorded one is still tried.
        shutil.rmtree(tmp_path / "n3wn3wn3w0")
        assert vscode.resolve_runtime(str(configured), str(cmd)) == str(configured)
        assert vscode.resolve_runtime(str(configured), None) == str(configured)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to run the wrapper")
class TestNodeWrapper:
    """The Windows wrapper, run with Node as VS Code's Copilot SDK runs a `.js` runtime path."""

    @staticmethod
    def _run(tmp_path, command: list[str], *args: str) -> subprocess.CompletedProcess:
        script = vscode._node_wrapper_script(command)
        wrapper = tmp_path / "ug-copilot-runtime-0.js"
        wrapper.write_text(script)
        return subprocess.run(
            ["node", str(wrapper), *args], capture_output=True, text=True, timeout=30
        )

    def test_runs_the_command_with_vscodes_arguments_and_its_exit_code(self, tmp_path):
        echo = [
            sys.executable,
            "-c",
            f"import os, sys; print(os.environ[{copilot.RUNTIME_WRAPPER_PID_ENV!r}].isdigit());"
            " print(sys.argv[1:]); sys.exit(3)",
        ]

        done = self._run(
            tmp_path,
            [*echo, "--runtime", r"C:\VS Code\copilot-runtime.exe", "--"],
            "--headless",
            "--stdio",
            'a "quoted" arg',
        )

        assert done.returncode == 3
        has_pid, argv = done.stdout.strip().splitlines()
        assert has_pid == "True"
        assert argv == str(
            [
                "--runtime",
                r"C:\VS Code\copilot-runtime.exe",
                "--",
                "--headless",
                "--stdio",
                'a "quoted" arg',
            ]
        )

    def test_passes_stdio_through(self, tmp_path):
        cat = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
        script = vscode._node_wrapper_script(cat)
        wrapper = tmp_path / "w.js"
        wrapper.write_text(script)

        done = subprocess.run(
            ["node", str(wrapper)], input="jsonrpc", capture_output=True, text=True, timeout=30
        )

        assert done.stdout == "JSONRPC"

    def test_a_command_that_cant_start_says_how_to_fix_it(self, tmp_path):
        missing = str(tmp_path / "no-such-ug")

        done = self._run(tmp_path, [missing, "copilot-seat-runtime"])

        assert done.returncode == 1
        assert f"[ucode] Couldn't start {missing}. Rerun `ug configure --agents copilot`." in (
            done.stderr
        )
        assert done.stdout == ""


class TestCopilotModels:
    """With the Copilot CLI's catalog in state, VS Code gets the same models and the managed setup."""

    SONNET = "system.ai.claude-sonnet-5-5"
    GPT = "system.ai.gpt-6-luna"

    def _state(self, **managed) -> dict:
        saved = {"models": [self.SONNET, self.GPT], "limits": {self.GPT: [922_000, 128_000]}}
        return {**STATE, copilot.CATALOG_STATE_KEY: saved, **managed}

    def test_uses_the_catalog_configure_saved_with_its_limits(self, code):
        assert vscode.configure(self._state())

        assert [model["id"] for model in code.config["models"]] == [self.SONNET, self.GPT]
        assert code.config["models"][1]["maxInputTokens"] == 794_000
        assert code.config["models"][1]["maxOutputTokens"] == 128_000
        assert "headers" not in code.config

    def test_uses_only_the_managed_list_and_says_what_it_skips(self, code, capsys):
        state = self._state(copilot_static_models=[self.GPT, "system.ai.gemini-4-argon"])

        assert vscode.configure(state)

        assert [model["id"] for model in code.config["models"]] == [self.GPT]
        assert code.config["models"][0]["maxInputTokens"] == 794_000
        assert "Skipping system.ai.gemini-4-argon from your workspace's managed config" in (
            _printed(capsys)
        )

    def test_a_traced_workspace_gets_one_trace_receiver_across_configures(self, code):
        state = self._state(copilot_otel_tracing=True)

        assert vscode.configure(state)
        first = code.config["otel"]
        assert vscode.configure(state)

        assert code.config["otel"] == first
        assert state[vscode.OTEL_RECEIVER_STATE_KEY] == first
        assert 0 < first["port"] < 65536
        assert re.fullmatch(r"[\w-]{20,}", first["secret"])

    def test_an_untraced_workspace_gets_no_trace_receiver(self, code):
        assert vscode.configure(self._state())

        assert "otel" not in code.config

    @pytest.mark.parametrize("default", [GPT, "gpt-6-luna"])
    def test_passes_the_managed_default_model_as_the_catalog_names_it(self, code, default):
        assert vscode.configure(self._state(copilot_default_model=default))

        assert code.config["defaultModel"] == self.GPT

    @pytest.mark.parametrize(
        "managed", [{}, {"copilot_default_model": "system.ai.claude-opus-5-5"}]
    )
    def test_passes_no_default_model_the_catalog_lacks(self, code, managed):
        assert vscode.configure(self._state(**managed))

        assert "defaultModel" not in code.config

    def test_writes_the_managed_headers_without_the_ones_ug_sets(self, code):
        state = self._state(
            copilot_http_headers={
                " X-Team ": "ml",
                "Authorization": "Bearer x",
                "content-type": "text/plain",
                "databricks-model-provider-service": "main.default.other",
                "X-Bad": 3,
            },
        )

        assert vscode.configure(state)

        assert code.config["headers"] == {"X-Team": "ml"}


class TestRevert:
    def test_uninstalls_the_extension(self, code):
        vscode.configure(STATE)

        assert vscode.revert() == "removed"

        assert code.calls[-1] == ("--uninstall-extension", vscode.EXTENSION_ID)
        assert not code.folder.exists()

    def test_runs_no_code_command_when_the_extension_isnt_installed(self, code):
        assert vscode.revert() == "unchanged"

        assert code.calls == []

    def test_skips_a_folder_vscode_left_behind_after_an_uninstall(self, code):
        (code.extensions / f"{vscode.EXTENSION_ID}-0.0.1").mkdir(parents=True)

        assert vscode.revert() == "unchanged"

        assert code.calls == [("--list-extensions", "--show-versions")]

    def test_uninstalls_through_the_insiders_command_for_an_insiders_install(self, code):
        code.extensions = vscode.extensions_dir(".vscode-insiders")
        vscode.configure(STATE)

        assert vscode.revert() == "removed"

        assert code.paths[-1] == Path("/fake/bin/code-insiders")


class TestRevertSettings:
    """Before uninstalling, revert removes the user settings the extension wrote."""

    GPT = "system.ai.gpt-6-luna"

    def _configure(self, code) -> dict[str, object]:
        """Configure a traced workspace with a managed default model; returns what the extension
        then writes to the user settings."""
        assert vscode.configure(
            {**STATE, "copilot_otel_tracing": True, "copilot_default_model": self.GPT}
        )
        otel = code.config["otel"]
        endpoint = f"http://127.0.0.1:{otel['port']}/{otel['secret']}"
        return {
            "github.copilot.chat.otel.enabled": True,
            "github.copilot.chat.otel.exporterType": "otlp-http",
            "github.copilot.chat.otel.protocol": "http/protobuf",
            "github.copilot.chat.otel.otlpEndpoint": endpoint,
            "chat.agentHost.otel.enabled": True,
            "chat.agentHost.otel.exporterType": "otlp-http",
            "chat.agentHost.otel.otlpEndpoint": endpoint,
            "chat.defaultModel": self.GPT,
        }

    def _settings(self, values: dict, profile: str | None = None) -> Path:
        user = vscode.user_dir(insiders=False)
        path = user / "profiles" / profile / "settings.json" if profile else user / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values))
        return path

    def test_removes_what_the_extension_wrote_then_uninstalls(self, code):
        written = self._configure(code)
        settings = self._settings({"editor.fontSize": 14, **written})
        profile = self._settings({**written, "editor.tabSize": 2}, profile="-1a2b3c")

        assert vscode.revert() == "removed"

        assert json.loads(settings.read_text()) == {"editor.fontSize": 14}
        assert json.loads(profile.read_text()) == {"editor.tabSize": 2}
        assert code.calls[-1] == ("--uninstall-extension", vscode.EXTENSION_ID)

    def test_leaves_what_the_developer_changed_since(self, code):
        written = self._configure(code)
        mine = {
            "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318",
            "chat.defaultModel": "system.ai.claude-sonnet-5-5",
        }
        settings = self._settings({**written, **mine})

        assert vscode.revert() == "removed"

        # The local agent now exports to the developer's collector, so its group stays whole.
        assert json.loads(settings.read_text()) == {
            "github.copilot.chat.otel.enabled": True,
            "github.copilot.chat.otel.exporterType": "otlp-http",
            "github.copilot.chat.otel.protocol": "http/protobuf",
            **mine,
        }

    def test_leaves_settings_with_comments_and_says_what_to_remove(self, code, capsys):
        self._configure(code)
        path = vscode.user_dir(insiders=False) / "settings.json"
        path.parent.mkdir(parents=True)
        text = '{\n  // mine\n  "chat.defaultModel": "system.ai.gpt-6-luna"\n}\n'
        path.write_text(text)

        assert vscode.revert() == "removed"

        assert path.read_text() == text
        printed = _printed(capsys)
        assert "Couldn't edit" in printed
        assert "chat.agentHost.otel.otlpEndpoint" in printed
        assert "chat.defaultModel" in printed

    def test_leaves_settings_alone_for_an_install_that_wrote_none(self, code):
        assert vscode.configure(STATE)
        text = '{"chat.defaultModel": "system.ai.gpt-6-luna", "chat.agentHost.otel.enabled": true}'
        path = self._settings({})
        path.write_text(text)

        assert vscode.revert() == "removed"

        assert path.read_text() == text

    @pytest.mark.parametrize(
        ("system", "insiders", "expected"),
        [
            (OS.LINUX, False, ".config/Code/User"),
            (OS.MACOS, True, "Library/Application Support/Code - Insiders/User"),
            (OS.WINDOWS, False, "AppData/Roaming/Code/User"),
        ],
    )
    def test_finds_the_user_folder(self, monkeypatch, system, insiders, expected):
        monkeypatch.setattr(vscode, "current_os", lambda: system)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        assert REAL_USER_DIR(insiders=insiders) == Path.home() / expected


def _desktop(
    root: Path,
    *,
    layout: str = "code",
    name: str = "Code",
    version: str = "1.147.0",
    targets: tuple[str, ...] = (),
    command: str = "code",
) -> Path:
    """A fake VS Code install under ``root``: `bin/<command>`, `resources/app/product.json` and a
    bundled runtime per target. Returns the `code` command."""
    app = root / layout / "resources" / "app"
    app.mkdir(parents=True)
    (app / "product.json").write_text(json.dumps({"nameShort": name, "version": version}))
    for target in targets:
        runtime = (
            app
            / "node_modules.asar.unpacked/@github"
            / f"copilot-sdk-{target}/prebuilds/{target}"
            / "copilot-runtime"
        )
        runtime.parent.mkdir(parents=True)
        runtime.write_text("")
    code = root / layout / "bin" / command
    code.parent.mkdir(parents=True, exist_ok=True)
    code.write_text("")
    return code


def _on_path(monkeypatch, *dirs: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(d) for d in dirs))


class TestFindCode:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch):
        monkeypatch.setattr(vscode, "find_code", REAL_FIND_CODE)
        monkeypatch.setattr(vscode, "_find_command", REAL_FIND_COMMAND)
        monkeypatch.setattr(vscode, "find_install", REAL_FIND_INSTALL)
        monkeypatch.setattr(vscode, "first_code_on_path", REAL_FIRST_CODE_ON_PATH)
        monkeypatch.setattr(vscode, "current_os", lambda: OS.LINUX)
        monkeypatch.setattr(vscode, "_install_locations", lambda command: [])
        monkeypatch.delenv("UG_VSCODE_CODE", raising=False)

    def test_prefers_code_on_path(self, monkeypatch, tmp_path):
        code = _desktop(tmp_path)
        _on_path(monkeypatch, code.parent)

        assert vscode.find_code() == code
        assert vscode.first_code_on_path() == code

    def test_falls_back_to_insiders(self, monkeypatch, tmp_path):
        insiders = _desktop(tmp_path, name="Code - Insiders", command="code-insiders")
        _on_path(monkeypatch, insiders.parent)

        assert vscode.find_code() == insiders
        assert vscode.find_install().insiders

    def test_looks_in_both_macos_application_folders(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        monkeypatch.setattr(vscode.Path, "home", lambda: tmp_path)
        app = "Visual Studio Code.app/Contents/Resources/app/bin/code"

        assert REAL_INSTALL_LOCATIONS("code") == [
            Path("/Applications") / app,
            tmp_path / "Applications" / app,
        ]

    def test_linux_install_locations_cover_snap_and_flatpak(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.Path, "home", lambda: tmp_path)

        locations = REAL_INSTALL_LOCATIONS("code")
        assert Path("/snap/code/current/usr/share/code/bin/code") in locations
        assert (
            Path(
                "/var/lib/flatpak/app/com.visualstudio.code/current/active/files/extra/vscode/bin/code"
            )
            in locations
        )
        assert (
            tmp_path
            / ".local/share/flatpak/app/com.visualstudio.code/current/active/files/extra/vscode/bin/code"
            in locations
        )

    def test_falls_back_to_the_first_install_location_that_exists(self, monkeypatch, tmp_path):
        _on_path(monkeypatch, tmp_path / "empty")
        installed = _desktop(tmp_path / "opt")
        monkeypatch.setattr(
            vscode,
            "_install_locations",
            lambda command: [tmp_path / "missing/code", installed] if command == "code" else [],
        )

        assert vscode.find_code() == installed

    def test_finds_the_windows_user_install(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.WINDOWS)
        monkeypatch.setattr(vscode, "_install_locations", REAL_INSTALL_LOCATIONS)
        _on_path(monkeypatch, tmp_path / "empty")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        root = tmp_path / "Programs/Microsoft VS Code"
        (root / "resources/app").mkdir(parents=True)
        (root / "resources/app/product.json").write_text(json.dumps({"nameShort": "Code"}))
        code_cmd = root / "bin/code.cmd"
        code_cmd.parent.mkdir(parents=True)
        code_cmd.write_text("@echo off\r\n")

        assert vscode.find_code() == code_cmd

    def test_none_when_vscode_isnt_installed(self, monkeypatch, tmp_path):
        _on_path(monkeypatch, tmp_path / "empty")

        assert vscode.find_code() is None
        assert vscode.first_code_on_path() is None

    def test_skips_another_products_code_on_path(self, monkeypatch, tmp_path):
        codium = _desktop(tmp_path / "codium", name="VSCodium")
        real = _desktop(tmp_path / "real")
        _on_path(monkeypatch, codium.parent, real.parent)

        assert vscode.find_code() == real
        assert vscode.first_code_on_path() == codium
        assert vscode.describe_command(codium) == "VSCodium"

    def test_skips_the_remote_server_command(self, monkeypatch, tmp_path):
        server = tmp_path / ".vscode-server/bin/abc123/bin/remote-cli/code"
        server.parent.mkdir(parents=True)
        server.write_text("")
        (tmp_path / ".vscode-server/bin/abc123/product.json").write_text('{"nameShort": "Code"}')
        real = _desktop(tmp_path / "real")
        _on_path(monkeypatch, server.parent, real.parent)

        assert vscode.find_code() == real
        assert vscode.describe_command(server) == "VS Code Server's command"

    def test_skips_a_stale_link(self, monkeypatch, tmp_path):
        stale = tmp_path / "usr/local/bin/code"
        stale.parent.mkdir(parents=True)
        stale.symlink_to(tmp_path / "gone/Visual Studio Code.app/Contents/Resources/app/bin/code")
        _on_path(monkeypatch, stale.parent)
        installed = _desktop(tmp_path / "Applications")
        monkeypatch.setattr(vscode, "_install_locations", lambda command: [installed])

        assert vscode.find_code() == installed
        assert vscode.describe_command(stale) == "a link to a VS Code that isn't there anymore"

    def test_prefers_the_install_with_a_runtime_for_the_seat_relay(self, monkeypatch, tmp_path):
        old = _desktop(tmp_path / "old", version="1.140.0")
        new = _desktop(tmp_path / "new", targets=("linux-x64",))
        _on_path(monkeypatch, old.parent, new.parent)

        assert vscode.find_install().code == old
        assert vscode.find_install(prefer_runtime=True).code == new
        assert vscode.find_install(prefer_runtime=True).runtime() is not None

    def test_override_wins_and_a_bad_one_fails(self, monkeypatch, tmp_path):
        on_path = _desktop(tmp_path / "a")
        other = _desktop(tmp_path / "b")
        _on_path(monkeypatch, on_path.parent)
        monkeypatch.setenv("UG_VSCODE_CODE", str(other))
        assert vscode.find_code() == other

        monkeypatch.setenv("UG_VSCODE_CODE", str(tmp_path / "nowhere/code"))
        with pytest.raises(RuntimeError, match="wasn't found"):
            vscode.find_code()
        codium = _desktop(tmp_path / "codium", name="VSCodium")
        monkeypatch.setenv("UG_VSCODE_CODE", str(codium))
        with pytest.raises(RuntimeError, match="VSCodium, not the desktop VS Code"):
            vscode.find_code()


class TestInstallToConfigure:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, code, monkeypatch, tmp_path):
        # After `code`, which stubs the lookup out.
        monkeypatch.setattr(vscode, "find_install", REAL_FIND_INSTALL)
        monkeypatch.setattr(vscode, "first_code_on_path", REAL_FIRST_CODE_ON_PATH)
        monkeypatch.setattr(vscode, "current_os", lambda: OS.LINUX)
        monkeypatch.setattr(vscode, "_install_locations", lambda command: [])
        monkeypatch.setattr(vscode.platform, "machine", lambda: "arm64")
        monkeypatch.delenv("UG_VSCODE_CODE", raising=False)
        monkeypatch.setattr(vscode, "_requested_code", None)
        _on_path(monkeypatch, tmp_path / "empty")

    def test_says_what_code_on_path_is_when_no_desktop_vscode_exists(
        self, monkeypatch, tmp_path, capsys
    ):
        codium = _desktop(tmp_path / "codium", name="VSCodium")
        _on_path(monkeypatch, codium.parent)

        assert not vscode.configure(STATE)

        printed = _printed(capsys)
        assert "is VSCodium, and no desktop VS Code was found" in printed
        assert "--vscode-code <path>" in printed

    def test_notes_when_it_uses_another_install_than_code_on_path(
        self, code, monkeypatch, tmp_path, capsys
    ):
        codium = _desktop(tmp_path / "codium", name="VSCodium")
        real = _desktop(tmp_path / "real")
        _on_path(monkeypatch, codium.parent, real.parent)

        assert vscode.configure(STATE)

        printed = _printed(capsys)
        assert "Using VS Code at" in printed and "is VSCodium" in printed

    def test_is_quiet_when_code_on_path_is_the_desktop_vscode(
        self, code, monkeypatch, tmp_path, capsys
    ):
        real = _desktop(tmp_path / "real")
        _on_path(monkeypatch, real.parent)

        assert vscode.configure(STATE)

        assert "Using VS Code at" not in _printed(capsys)

    def test_an_intel_vscode_on_an_apple_silicon_mac_routes(
        self, code, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        monkeypatch.setattr(vscode, "uses_seat_relay", lambda state, provider, tool: True)
        monkeypatch.setattr(vscode, "ug_binary", lambda: "/bin/ug")
        intel = _desktop(
            tmp_path / "Applications",
            layout="Visual Studio Code.app/Contents/Resources",
            targets=("darwin-x64",),
        )
        _on_path(monkeypatch, intel.parent)

        assert vscode.configure(STATE, provider="main.ai.copilot_seats")

        assert "routing Copilot through main.ai.copilot_seats" in _printed(capsys)

    def test_a_universal_build_prefers_the_hosts_architecture(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        universal = _desktop(
            tmp_path,
            layout="Visual Studio Code.app/Contents/Resources",
            targets=("darwin-x64", "darwin-arm64"),
        )

        assert vscode.bundled_runtime(universal).parent.name == "darwin-arm64"
        monkeypatch.setattr(vscode.platform, "machine", lambda: "x86_64")
        assert vscode.bundled_runtime(universal).parent.name == "darwin-x64"

    def test_a_desktop_vscode_without_the_runtime_names_itself(
        self, code, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(vscode, "uses_seat_relay", lambda state, provider, tool: True)
        old = _desktop(tmp_path / "old", version="1.140.0")
        _on_path(monkeypatch, old.parent)

        assert not vscode.configure(STATE, provider="main.ai.copilot_seats")

        printed = _printed(capsys)
        assert "VS Code 1.140.0 at" in printed and "has no bundled Copilot runtime" in printed
        assert "Update VS Code" in printed

    def test_the_override_is_used_and_recorded(self, code, monkeypatch, tmp_path):
        on_path = _desktop(tmp_path / "a", name="VSCodium")
        other = _desktop(tmp_path / "b")
        _on_path(monkeypatch, on_path.parent)
        vscode.request_code(str(other))
        state = dict(STATE)

        assert vscode.configure(state)

        assert state[vscode.CODE_PATH_STATE_KEY] == str(other)

    def test_a_bad_override_fails_loudly(self, code, monkeypatch, tmp_path, capsys):
        vscode.request_code(str(tmp_path / "nowhere/code"))

        assert not vscode.configure(STATE)

        assert "wasn't found at" in _printed(capsys)


class TestNextSteps:
    @pytest.mark.parametrize(
        ("system", "restart"),
        [
            (OS.MACOS, "quit VS Code with Cmd+Q, even if no window is open, and open it again"),
            (OS.WINDOWS, "close every VS Code window and open VS Code again"),
            (OS.LINUX, "close every VS Code window and open VS Code again"),
        ],
    )
    def test_says_how_to_restart_on_the_os(self, monkeypatch, system, restart):
        monkeypatch.setattr(vscode, "current_os", lambda: system)

        assert vscode.next_steps() == (
            f"In VS Code: {restart}, then open Chat, click the model picker and choose a Databricks "
            "model."
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


def _stub_copilot_setup(monkeypatch) -> list[dict]:
    """Make the Copilot CLI setup a no-op and record each VS Code setup's model source."""
    configured: list[dict] = []
    monkeypatch.setattr(agents_mod, "_configure_one", lambda tool, state, *a, **k: state)
    monkeypatch.setattr(
        vscode, "configure", lambda state, **source: configured.append(source) or True
    )
    return configured


@pytest.mark.parametrize(
    ("saved", "source"),
    [
        ({}, {"provider": None, "location": None}),
        (
            {"provider_services": {"copilot": "main.default.mps"}},
            {"provider": "main.default.mps", "location": None},
        ),
        (
            {"model_locations": {"copilot": "main.models"}},
            {"provider": None, "location": "main.models"},
        ),
    ],
)
def test_configuring_copilot_sets_up_vscode_with_the_same_models(monkeypatch, saved, source):
    configured = _stub_copilot_setup(monkeypatch)

    agents_mod.configure_selected_tools({**STATE, **saved}, ["claude", "copilot"])

    assert configured == [source]


def test_a_failed_copilot_setup_leaves_vscode_alone(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agents_mod, "_configure_one", fail)
    monkeypatch.setattr(vscode, "configure", lambda *a, **k: pytest.fail("VS Code was set up"))

    state = agents_mod.configure_selected_tools(dict(STATE), ["copilot"])

    assert state["last_configured_tools"] == []


def test_configuring_copilot_alone_sets_up_vscode_only_from_configure(monkeypatch):
    configured = _stub_copilot_setup(monkeypatch)
    monkeypatch.setattr(agents_mod, "check_gateway_endpoint", lambda *args: True)

    # A launch that configures copilot first leaves VS Code alone; `ug configure --agent` doesn't.
    agents_mod.configure_single_tool("copilot", dict(STATE))
    agents_mod.configure_single_tool("copilot", dict(STATE), with_vscode=True)

    assert configured == [{"provider": None, "location": None}]


@pytest.mark.parametrize("skip", ["no_vscode", "dry_run"])
def test_no_vscode_and_a_dry_run_leave_vscode_alone(monkeypatch, capsys, skip):
    # `--dry-run` sets a module-wide flag; this puts it back so later tests write files again.
    monkeypatch.setattr(config_io, "_dry_run", skip == "dry_run")
    agents_mod.request_vscode_with_copilot(skip != "no_vscode")
    monkeypatch.setattr(vscode, "configure", lambda *a, **k: pytest.fail("VS Code was set up"))

    agents_mod.configure_vscode(STATE, provider=None, parent_schema=None)

    assert ("VS Code isn't changed" in capsys.readouterr().out) == (skip == "dry_run")


MANAGED_OFF = {**STATE, "copilot_vscode_integration": False}


@pytest.mark.parametrize("no_vscode", [False, True])
def test_managed_vscode_off_removes_an_earlier_setup(code, capsys, no_vscode):
    vscode.configure(STATE)
    agents_mod.request_vscode_with_copilot(not no_vscode)

    agents_mod.configure_vscode(MANAGED_OFF, provider=None, parent_schema=None)

    assert code.calls[-1] == ("--uninstall-extension", vscode.EXTENSION_ID)
    assert not code.folder.exists()
    assert "the VS Code extension was removed" in _printed(capsys)


def test_managed_vscode_off_skips_vscode_without_an_earlier_setup(code, capsys):
    agents_mod.configure_vscode(MANAGED_OFF, provider=None, parent_schema=None)

    assert code.calls == []
    assert "turns off GitHub Copilot in VS Code, so VS Code was skipped" in _printed(capsys)


def test_managed_vscode_off_changes_nothing_in_a_dry_run(code, monkeypatch, capsys):
    vscode.configure(STATE)
    calls = len(code.calls)
    monkeypatch.setattr(config_io, "_dry_run", True)

    agents_mod.configure_vscode(MANAGED_OFF, provider=None, parent_schema=None)

    assert len(code.calls) == calls
    assert code.folder.exists()
    assert "VS Code isn't changed" in capsys.readouterr().out


def test_configure_passes_copilot_its_model_source_and_no_vscode(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)
    monkeypatch.setattr(
        "ucode.cli.configure_workspace_command", lambda **kwargs: calls.append(kwargs) or 0
    )

    result = CliRunner().invoke(
        app,
        [
            "configure",
            "--agents",
            "copilot",
            "--workspace",
            WS,
            "--provider",
            "main.default.mps",
            "--no-vscode",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["selected_tools"] == ["copilot"]
    assert calls[0]["provider"] == "main.default.mps"
    assert "model_location" not in calls[0]
    assert agents_mod._vscode_with_copilot is False


def _stub_workspace_setup(monkeypatch, state: dict, managed: dict | None) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
    monkeypatch.setattr(cli_mod, "_refresh_configured_policy", lambda s: (managed, False))
    monkeypatch.setattr(cli_mod, "check_gateway_endpoint", lambda *args: True)
    monkeypatch.setattr(cli_mod, "install_tool_binary", lambda *a, **k: True)
    monkeypatch.setattr(
        cli_mod,
        "configure_selected_tools",
        lambda s, tools, **k: (
            seen.append({"state": dict(s), "tools": tools, **k})
            or {**s, "last_configured_tools": list(tools)}
        ),
    )
    monkeypatch.setattr(cli_mod, "save_state", lambda s: None)
    return seen


@pytest.mark.parametrize("tools", [["copilot"], ["claude", "copilot"]], ids=["alone", "mixed"])
def test_configure_saves_copilots_model_location_for_its_launches(monkeypatch, tools):
    state = {**STATE, "provider_services": {"copilot": "old.default.mps"}}
    seen = _stub_workspace_setup(monkeypatch, state, None)
    checked: list[str] = []
    monkeypatch.setattr(
        copilot, "location_catalog", lambda state, schema: checked.append(schema) or ["a model"]
    )

    saved: list[dict] = []
    monkeypatch.setattr(cli_mod, "save_state", lambda s: saved.append(s))
    cli_mod.configure_workspace_command(
        selected_tools=tools, workspaces=[(WS, None)], model_location="main.models"
    )

    assert checked == ["main.models"]
    assert seen[0]["parent_schemas"] == dict.fromkeys(tools, "main.models")
    assert "copilot" not in saved[-1].get("provider_services", {})
    assert saved[-1]["model_locations"]["copilot"] == "main.models"


def test_configure_saves_copilots_provider_for_its_launches(monkeypatch):
    state = {**STATE, "model_locations": {"copilot": "old.models"}}
    seen = _stub_workspace_setup(monkeypatch, state, None)
    monkeypatch.setattr(copilot, "uses_seat_relay", lambda *a, **k: False)
    monkeypatch.setattr(copilot, "provider_service_catalog", lambda *a, **k: ["a model"])
    saved: list[dict] = []
    monkeypatch.setattr(cli_mod, "save_state", lambda s: saved.append(s))

    cli_mod.configure_workspace_command(
        selected_tools=["copilot"], workspaces=[(WS, None)], provider="main.default.mps"
    )

    assert seen[0]["providers"] == {"copilot": "main.default.mps"}
    assert saved[-1]["provider_services"]["copilot"] == "main.default.mps"
    assert "copilot" not in saved[-1].get("model_locations", {})


@pytest.mark.parametrize(
    "source",
    [{"provider": "main.default.typo"}, {"model_location": "main.empty"}],
    ids=["provider", "location"],
)
@pytest.mark.parametrize("tools", [["copilot"], ["claude", "copilot"]], ids=["alone", "mixed"])
def test_configure_keeps_copilots_source_when_the_new_one_fails(monkeypatch, source, tools):
    def unusable(*args, **kwargs):
        raise RuntimeError("not usable")

    state = {**STATE, "provider_services": {"copilot": "old.default.mps"}}
    seen = _stub_workspace_setup(monkeypatch, state, None)
    monkeypatch.setattr(copilot, "uses_seat_relay", lambda *a, **k: False)
    monkeypatch.setattr(copilot, "provider_service_catalog", unusable)
    monkeypatch.setattr(copilot, "location_catalog", unusable)

    with pytest.raises(RuntimeError, match="not usable"):
        cli_mod.configure_workspace_command(selected_tools=tools, workspaces=[(WS, None)], **source)

    assert seen == []
    assert state["provider_services"] == {"copilot": "old.default.mps"}
    assert "model_locations" not in state


def test_managed_configure_hands_copilot_the_vscode_switch(monkeypatch):
    seen = _stub_workspace_setup(
        monkeypatch,
        dict(STATE),
        {"enabled_agents": {"copilot": {"vscode_integration_enabled": False}}},
    )

    cli_mod.configure_workspace_command(selected_tools=None, workspaces=[(WS, None)])

    assert seen[0]["state"]["copilot_vscode_integration"] is False


@pytest.mark.parametrize(
    "source", [{"provider": "main.default.mps"}, {"model_location": "main.models"}]
)
def test_configure_refuses_a_copilot_model_source_on_a_managed_workspace(monkeypatch, source):
    seen = _stub_workspace_setup(
        monkeypatch, dict(STATE), {"enabled_agents": {"copilot": {"model_config": {}}}}
    )

    with pytest.raises(RuntimeError, match="managed"):
        cli_mod.configure_workspace_command(
            selected_tools=["copilot"], workspaces=[(WS, None)], **source
        )

    assert seen == []


def test_single_agent_copilot_checks_then_saves_its_source(monkeypatch):
    state = dict(STATE)
    monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])
    monkeypatch.setattr(cli_mod, "_refresh_configured_policy", lambda s: (None, False))
    monkeypatch.setattr(cli_mod, "_read_policy_for_other_agent", lambda s: (None, False))
    order: list[str] = []
    monkeypatch.setattr(
        copilot, "location_catalog", lambda s, schema: order.append("check") or ["m"]
    )
    monkeypatch.setattr(
        cli_mod,
        "configure_single_tool",
        lambda tool, s, **k: order.append(f"configure:{k['parent_schema']}") or s,
    )
    saved: list[dict] = []
    monkeypatch.setattr(cli_mod, "save_state", lambda s: saved.append(s))

    cli_mod._setup_single_agent(
        [(WS, None)], "copilot", apply_managed=False, model_location="main.models"
    )

    assert order == ["check", "configure:main.models"]
    assert saved[-1]["model_locations"]["copilot"] == "main.models"


def test_single_agent_copilot_bad_source_is_not_configured(monkeypatch):
    state = dict(STATE)
    monkeypatch.setattr(cli_mod, "_configure_shared_workspace_states", lambda *a, **k: [state])

    def unusable(*args, **kwargs):
        raise RuntimeError("not usable")

    monkeypatch.setattr(copilot, "uses_seat_relay", lambda *a, **k: False)
    monkeypatch.setattr(copilot, "provider_service_catalog", unusable)
    monkeypatch.setattr(
        cli_mod, "configure_single_tool", lambda *a, **k: pytest.fail("copilot was configured")
    )

    with pytest.raises(RuntimeError, match="not usable"):
        cli_mod._setup_single_agent(
            [(WS, None)], "copilot", apply_managed=False, provider="main.default.typo"
        )


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--provider", "a.b.c"], "--provider requires --agent or --agents"),
        (["--agents", "cursor", "--provider", "a.b.c"], "isn't supported for the selected agents"),
        (["--agent", "gemini", "--model-location", "a.b"], "Supported: Claude Code, Codex, GitHub"),
        (
            ["--agents", "copilot", "--provider", "a.b.c", "--model-location", "a.b"],
            "cannot be combined",
        ),
        (["--agents", "copilot", "--model-location", "a.b.c"], "must be <catalog>.<schema>"),
    ],
)
def test_configure_refuses_a_model_source_copilot_cant_take(monkeypatch, args, error):
    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)
    monkeypatch.setattr(
        "ucode.cli._configure_shared_workspace_states",
        lambda *a, **k: pytest.fail("the workspace was configured"),
    )

    result = CliRunner().invoke(app, ["configure", "--workspace", WS, *args])

    assert result.exit_code != 0
    assert error in " ".join(result.output.split())


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        (["--agent", "copilot", "--model-location", "a.b"], {"tool": "copilot"}),
        (["--agents", "claude,copilot", "--model-location", "a.b"], {}),
        (["--agents", "copilot,cursor", "--provider", "a.b.c"], {}),
    ],
)
def test_configure_accepts_copilot_with_a_model_source(monkeypatch, args, kwargs):
    calls: list[dict] = []
    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)
    monkeypatch.setattr("ucode.cli.install_tool_binary", lambda *a, **k: True)
    monkeypatch.setattr(
        "ucode.cli.configure_workspace_command", lambda *a, **k: calls.append(k) or 0
    )

    result = CliRunner().invoke(app, ["configure", "--workspace", WS, *args])

    assert result.exit_code == 0, result.output
    assert calls
    assert "copilot_source" not in calls[0]
    assert calls[0].get("model_location") or calls[0].get("provider")


def test_vscode_is_not_an_agent_of_its_own(monkeypatch):
    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)

    result = CliRunner().invoke(app, ["configure", "--agents", "vscode", "--workspace", WS])

    assert result.exit_code != 0
    assert "Unsupported tool 'vscode'" in " ".join(result.output.split())
