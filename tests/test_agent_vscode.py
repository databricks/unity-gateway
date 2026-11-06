"""Tests for Copilot Chat in VS Code: `ug configure --agents copilot` sets it up, and revert."""

from __future__ import annotations

import json
import subprocess
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
REAL_RUN_CODE = vscode._run_code

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
        if args[0] == "--uninstall-extension":
            self.folder.rmdir()
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def code(monkeypatch):
    fake = FakeCode(vscode.extensions_dir(".vscode"))
    monkeypatch.setattr(vscode, "find_code", lambda: Path("/fake/bin/code"))
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
        assert "Next, in VS Code: 1. If VS Code is open," in printed

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
        assert "Skipping system.ai.gemini-4-argon from your workspace's managed config" in (
            _printed(capsys)
        )

    def test_writes_the_managed_headers_without_the_ones_ug_sets(self, code):
        state = self._state(
            copilot_http_headers={
                " X-Team ": "ml",
                "Authorization": "Bearer x",
                "content-type": "text/plain",
                "databricks-model-provider-service": "main.default.other",
                "X-Bad": 3,
            },
            copilot_default_model="claude-sonnet-5-5",
        )

        assert vscode.configure(state)

        assert code.config["headers"] == {"X-Team": "ml"}
        assert "defaultModel" not in code.config


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
    monkeypatch.setattr(copilot, "provider_service_catalog", unusable)
    monkeypatch.setattr(copilot, "location_catalog", unusable)

    with pytest.raises(RuntimeError, match="not usable"):
        cli_mod.configure_workspace_command(selected_tools=tools, workspaces=[(WS, None)], **source)

    assert seen == []
    assert state["provider_services"] == {"copilot": "old.default.mps"}
    assert "model_locations" not in state


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
