"""The marketplace predecessor must not bypass UG's shared routing controls."""

import json
import tomllib

import pytest
import tomlkit

from ucode import codex_config
from ucode.agents import claude
from ucode.smart_routing import orchestrator, v2


@pytest.mark.parametrize("launch_mode", ["direct", "relayed", "routing"])
@pytest.mark.parametrize("routing_enabled", ["0", "1"])
def test_claude_suppresses_legacy_plugins_without_changing_saved_settings(
    tmp_path, monkeypatch, launch_mode, routing_enabled
):
    monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, routing_enabled)
    user_plugin = "model-orchestrator@eng-plugin-marketplace-experimental"
    project_plugin = "model-orchestrator@project-marketplace"
    caller_plugin = "model-orchestrator@caller-marketplace"
    user_settings = claude.CLAUDE_USER_SETTINGS_PATH
    registry = user_settings.parent / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps({"version": 2, "plugins": {project_plugin: [{"scope": "project"}]}})
    )
    user_settings.write_text(
        json.dumps({"enabledPlugins": {user_plugin: True, "unrelated@marketplace": True}})
    )
    gateway_settings = tmp_path / "ucode-settings.json"
    gateway_settings.write_text(json.dumps({"apiKeyHelper": "gateway-helper"}))
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", gateway_settings)
    user_hook = {"hooks": [{"type": "command", "command": "user-policy"}]}
    caller = tmp_path / "caller-settings.json"
    caller.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    caller_plugin: True,
                    "unrelated@marketplace": True,
                    "model-orchestrator-extra@marketplace": True,
                },
                "hooks": {"UserPromptSubmit": [user_hook]},
            }
        )
    )
    before = {
        path: path.read_bytes() for path in (user_settings, registry, gateway_settings, caller)
    }
    args = ["--settings", str(caller), "--print", "hello"]

    if launch_mode == "routing":
        composed, remaining = claude._compose_v2_settings(args)
        assert composed["enabledPlugins"][user_plugin] is False
        argv = claude._build_claude_argv("claude", remaining, settings_override=composed)
    else:
        argv = claude._build_claude_argv("claude", args, relayed=launch_mode == "relayed")

    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["enabledPlugins"] == {
        user_plugin: False,
        project_plugin: False,
        caller_plugin: False,
        "unrelated@marketplace": True,
        "model-orchestrator-extra@marketplace": True,
    }
    assert settings["apiKeyHelper"] == "gateway-helper"
    assert settings["hooks"]["UserPromptSubmit"] == [user_hook]
    assert argv[-2:] == ["--print", "hello"]
    assert args == ["--settings", str(caller), "--print", "hello"]
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("custom_home", [False, True])
def test_claude_suppresses_installed_plugin_without_caller_settings(
    tmp_path, monkeypatch, custom_home
):
    config_dir = (
        tmp_path / "custom-claude" if custom_home else claude.CLAUDE_USER_SETTINGS_PATH.parent
    )
    registry = config_dir / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    name = "model-orchestrator@custom-marketplace"
    registry.write_text(json.dumps({"plugins": {name: [{"scope": "user"}]}}))
    if custom_home:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    gateway_settings = tmp_path / "ucode-settings.json"
    gateway_settings.write_text("{}")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", gateway_settings)

    argv = claude._build_claude_argv("claude", ["--print", "hello"])

    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"enabledPlugins": {name: False}}
    assert gateway_settings.read_text() == "{}"


def test_claude_without_legacy_plugin_keeps_settings_file_argument(tmp_path, monkeypatch):
    user_settings = claude.CLAUDE_USER_SETTINGS_PATH
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(
        json.dumps({"enabledPlugins": {"model-orchestrator-extra@marketplace": True}})
    )
    gateway_settings = tmp_path / "ucode-settings.json"
    gateway_settings.write_text("{}")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", gateway_settings)

    assert claude._build_claude_argv("claude", ["--print", "hello"]) == [
        "claude",
        "--settings",
        str(gateway_settings),
        "--print",
        "hello",
    ]


@pytest.mark.parametrize("custom_home", [False, True])
def test_codex_suppresses_legacy_plugins_from_saved_config_layers(
    tmp_path, monkeypatch, custom_home
):
    default_profile = codex_config.DEFAULT_CODEX_CONFIG_PATH
    profile = default_profile
    if custom_home:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
        profile = tmp_path / "custom-codex" / "ucode.config.toml"
        default_profile.parent.mkdir(parents=True)
        default_profile.write_text('[plugins."model-orchestrator@unused-home"]\nenabled = true\n')
    managed = tmp_path / "managed_config.toml"
    monkeypatch.setattr(codex_config, "codex_managed_config_path", lambda: managed)
    names = {
        managed: "model-orchestrator@managed-marketplace",
        profile: "model-orchestrator@profile-marketplace",
        profile.parent / "config.toml": "model-orchestrator@isaac-sync-user.marketplace",
    }
    for path, name in names.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            tomlkit.dumps(
                {
                    "plugins": {
                        name: {"enabled": True},
                        "unrelated@marketplace": {"enabled": True},
                        "model-orchestrator-extra@marketplace": {"enabled": True},
                    }
                }
            )
        )
    before = {path: path.read_bytes() for path in names}

    override = orchestrator.legacy_codex_plugin_config(default_profile)

    assert override == {"plugins": {name: {"enabled": False} for name in names.values()}}
    flag, value = codex_config.codex_config_args(override)
    assert flag == "--config"
    assert value.startswith("plugins={")
    assert tomllib.loads(value) == override
    assert {path: path.read_bytes() for path in before} == before


def test_codex_suppresses_project_plugins_from_nested_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "user"))
    project = tmp_path / "project"
    nested = project / "nested"
    names = {
        project / ".codex/config.toml": "model-orchestrator@project",
        nested / ".codex/config.toml": "model-orchestrator@nested",
    }
    for path, name in names.items():
        path.parent.mkdir(parents=True)
        path.write_text(
            tomlkit.dumps(
                {
                    "plugins": {name: {"enabled": True}, "unrelated@project": {"enabled": True}},
                    "hooks": {"UserPromptSubmit": [{"hooks": [{"command": "project-policy"}]}]},
                }
            )
        )
    before = {path: path.read_bytes() for path in names}

    override = orchestrator.legacy_codex_plugin_config(cwd=nested)

    assert override == {"plugins": {name: {"enabled": False} for name in names.values()}}
    assert {path: path.read_bytes() for path in before} == before


def test_codex_without_legacy_plugin_has_no_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        '[plugins."model-orchestrator-extra@marketplace"]\nenabled = true\n'
    )

    assert orchestrator.legacy_codex_plugin_config() == {}
