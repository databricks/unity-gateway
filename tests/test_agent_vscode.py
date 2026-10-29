"""Tests for `ug configure --agents vscode` and its revert."""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ucode import config_io
from ucode import databricks as databricks_module
from ucode.agents import copilot, vscode
from ucode.cli import app
from ucode.databricks import AnthropicModelCatalog
from ucode.managed_files import OS

# The conftest fixture stubs these out for every test; the ones here exercise the real lookups.
REAL_FIND_CODE = vscode.find_code
REAL_FIND_COMMAND = vscode._find_command
REAL_RUN_CODE = vscode._run_code

WS = "https://ws.cloud.databricks.com"
STATE = {"workspace": WS, "profile": "dev"}
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
        if args[0] == "--uninstall-extension":
            self.folder.rmdir()
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def code(monkeypatch):
    fake = FakeCode(vscode.extensions_dir(".vscode"))
    monkeypatch.setattr(vscode, "find_code", lambda: Path("/fake/bin/code"))
    monkeypatch.setattr(vscode, "_find_command", lambda command: Path(f"/fake/bin/{command}"))
    monkeypatch.setattr(vscode, "_run_code", fake)
    monkeypatch.setattr(vscode, "launch_token", lambda state, workspace: "token")
    monkeypatch.setattr(copilot, "launch_token", lambda state, workspace: "token")
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


class TestConfigure:
    def test_installs_the_extension_with_the_workspace_and_its_claude_and_gpt_models(
        self, code, capsys
    ):
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
                    "api": "anthropic_messages",
                    "maxInputTokens": 872_000,
                    "maxOutputTokens": 128_000,
                },
                {
                    "id": "system.ai.gpt-6-luna",
                    "name": "GPT 6 Luna",
                    "family": "gpt-6-luna",
                    "api": "responses",
                },
            ],
        }
        printed = _printed(capsys)
        assert (
            f"VS Code extension: installed {vscode.EXTENSION_ID} {VERSION} with 2 models" in printed
        )
        assert "Next, in VS Code: 1. If VS Code is open," in printed

    def test_models_with_the_same_label_show_their_ids(self, code, monkeypatch):
        monkeypatch.setattr(
            vscode,
            "discover_model_services",
            lambda workspace, token: (
                {},
                ["system.ai.gpt-6-luna", "main.models.gpt-6-luna", "system.ai.gpt-5-5"],
                [],
                [],
                None,
            ),
        )

        models = vscode.model_entries(STATE)

        assert [model["name"] for model in models] == [
            "main.models.gpt-6-luna",
            "system.ai.gpt-6-luna",
            "GPT 5.5",
        ]

    def test_a_provider_services_claude_targets_with_a_200k_cap_and_its_name(
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
                "name": "Claude Opus 4.8",
                "family": "claude-opus-4-8",
                "api": "anthropic_messages",
                "maxInputTokens": 168_000,
                "maxOutputTokens": 32_000,
            },
            {
                "id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "name": "Claude Sonnet 4.5",
                "family": "claude-sonnet-4-5",
                "api": "anthropic_messages",
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
            {"id": "gpt-5.5", "name": "GPT 5.5", "family": "gpt-5.5", "api": "responses"},
            {"id": "gpt-4.1", "name": "GPT 4.1", "family": "gpt-4.1", "api": "chat_completions"},
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

    def test_a_model_locations_services_with_a_200k_cap(self, code, monkeypatch):
        listed: list[str | None] = []

        def listing(workspace, token, *, parent_schema=None):
            listed.append(parent_schema)
            return AnthropicModelCatalog(
                model_ids=["main.models.claude-opus-5-5", "main.models.team-assistant"],
                model_id_to_display_name={},
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
                "api": "anthropic_messages",
                "maxInputTokens": 168_000,
                "maxOutputTokens": 32_000,
            },
            {
                "id": "main.models.team-assistant",
                "name": "Team Assistant",
                "family": "team-assistant",
                "api": "anthropic_messages",
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
        monkeypatch.setattr(
            vscode, "discover_model_services", lambda workspace, token: ({}, [], [], [], reason)
        )

        assert not vscode.configure({**STATE, "workspace": "https://other.cloud.databricks.com"})

        assert code.calls == []
        assert code.config == installed
        assert f"No Claude or GPT models for VS Code ({reason or 'the workspace has none'})" in (
            _printed(capsys)
        )

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


class TestFindCode:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch):
        monkeypatch.setattr(vscode, "find_code", REAL_FIND_CODE)
        monkeypatch.setattr(vscode, "_find_command", REAL_FIND_COMMAND)

    def test_prefers_code_on_path(self, monkeypatch):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: f"/usr/local/bin/{name}")

        assert vscode.find_code() == Path("/usr/local/bin/code")

    def test_falls_back_to_insiders(self, monkeypatch):
        monkeypatch.setattr(
            vscode.shutil,
            "which",
            lambda name: "/opt/bin/code-insiders" if name == "code-insiders" else None,
        )

        assert vscode.find_code() == Path("/opt/bin/code-insiders")

    def test_looks_in_both_macos_application_folders(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode, "current_os", lambda: OS.MACOS)
        monkeypatch.setattr(vscode.Path, "home", lambda: tmp_path)
        app = "Visual Studio Code.app/Contents/Resources/app/bin/code"

        assert vscode._install_locations("code") == [
            Path("/Applications") / app,
            tmp_path / "Applications" / app,
        ]

    def test_falls_back_to_the_first_install_location_that_exists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        installed = tmp_path / "bin/code"
        installed.parent.mkdir()
        installed.touch()
        monkeypatch.setattr(
            vscode,
            "_install_locations",
            lambda command: [tmp_path / "missing/code", installed] if command == "code" else [],
        )

        assert vscode.find_code() == installed

    def test_finds_the_windows_user_install(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode, "current_os", lambda: OS.WINDOWS)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        code_cmd = tmp_path / "Programs/Microsoft VS Code/bin/code.cmd"
        code_cmd.parent.mkdir(parents=True)
        code_cmd.touch()

        assert vscode.find_code() == code_cmd

    def test_none_when_vscode_isnt_installed(self, monkeypatch):
        monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
        monkeypatch.setattr(vscode, "_install_locations", lambda command: [])

        assert vscode.find_code() is None


class TestNextSteps:
    @pytest.mark.parametrize(
        ("system", "restart"),
        [
            (OS.MACOS, "If VS Code is open, quit it (Cmd+Q) and open it again."),
            (OS.WINDOWS, "If VS Code is open, close every VS Code window and open it again."),
            (OS.LINUX, "If VS Code is open, close every VS Code window and open it again."),
        ],
    )
    def test_says_how_to_restart_on_the_os(self, monkeypatch, system, restart):
        monkeypatch.setattr(vscode, "current_os", lambda: system)

        assert vscode.next_steps() == [
            f"1. {restart}",
            "2. Open Chat, click the model picker, and choose a Databricks model.",
        ]


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


def test_configure_agents_vscode_refuses_a_workspace_the_machine_isnt_managed_for(monkeypatch):
    selected: list[str] = []
    monkeypatch.setattr(
        "ucode.cli._configure_shared_workspace_states", lambda *args, **kwargs: [STATE]
    )
    monkeypatch.setattr("ucode.cli.load_state", lambda: STATE)
    monkeypatch.setattr(
        "ucode.cli.managed_state_workspace", lambda: "https://managed.cloud.databricks.com"
    )
    monkeypatch.setattr("ucode.cli.set_current_workspace", selected.append)
    monkeypatch.setattr(vscode, "configure", lambda state, **_: pytest.fail("VS Code was set up"))

    result = CliRunner().invoke(app, ["configure", "--agents", "vscode", "--workspace", WS])

    assert result.exit_code == 1
    assert "This machine is managed for https://managed.cloud.databricks.com" in " ".join(
        result.output.split()
    )
    assert selected == ["https://managed.cloud.databricks.com"]


@pytest.mark.parametrize("dry_run", [False, True])
def test_configure_agents_vscode_sets_up_the_workspace_then_vscode(monkeypatch, dry_run):
    # `--dry-run` sets a module-wide flag; this puts it back so later tests write files again.
    monkeypatch.setattr(config_io, "_dry_run", False)
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
    monkeypatch.setattr(
        vscode,
        "configure",
        lambda state, **source: configured.append({**state, **source}) or True,
    )

    args = [
        "configure",
        "--agents",
        "vscode",
        "--workspace",
        WS,
        "--provider",
        "main.default.mps",
        *(["--dry-run"] if dry_run else []),
    ]
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    assert shared[0]["tools"] == []
    # A dry run saves no workspace, so VS Code would be set up from stale or missing state.
    source = {"provider": "main.default.mps", "location": None}
    assert configured == ([] if dry_run else [{**STATE, **source}])
    assert ("VS Code isn't changed" in result.output) == dry_run


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--agents", "claude", "--provider", "a.b.c"], "work only with `--agents vscode`"),
        (["--provider", "a.b.c"], "work only with `--agents vscode`"),
        # A model agent alongside VS Code would silently keep the workspace's own models.
        (
            ["--agents", "claude,vscode", "--provider", "a.b.c"],
            "work only with `--agents vscode`",
        ),
        (
            ["--agents", "vscode", "--provider", "a.b.c", "--model-location", "a.b"],
            "cannot be used together",
        ),
        (["--agents", "vscode", "--model-location", "a.b.c"], "must be `<catalog>.<schema>`"),
    ],
)
def test_configure_refuses_a_model_source_vscode_cant_take(monkeypatch, args, error):
    monkeypatch.setattr("ucode.cli.install_databricks_cli", lambda: None)
    monkeypatch.setattr(
        "ucode.cli._configure_shared_workspace_states",
        lambda *a, **k: pytest.fail("the workspace was configured"),
    )

    result = CliRunner().invoke(app, ["configure", "--workspace", WS, *args])

    assert result.exit_code != 0
    assert error in " ".join(result.output.split())
