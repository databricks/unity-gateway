"""Opt-in desktop sync keeps unrelated app settings while applying managed routes."""

from __future__ import annotations

import json

import tomlkit

from ucode import desktop_config
from ucode.agents import codex

WORKSPACE = "https://ws.example.com"


def test_codex_desktop_uses_managed_default_and_keeps_user_settings(monkeypatch, tmp_path):
    path = tmp_path / "codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(
        'model = "system.ai.gpt-6-sol"\n'
        'model_provider = "Databricks"\n'
        'notify = ["terminal-notifier"]\n'
        '[model_providers.Other]\nname = "Other"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(codex, "_legacy_config_path", lambda: path)
    monkeypatch.setattr(desktop_config, "CODEX_DESKTOP_BACKUP_PATH", tmp_path / "codex.backup")
    model = "platform_ai_gateway_dbricks.models.openai-gpt-5-6-luna"

    desktop_config.sync_codex_desktop(
        {"workspace": WORKSPACE, "profile": "ws", "codex_default_model": model}
    )

    saved = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert saved["model"] == model
    assert saved["model_provider"] == "Databricks"
    assert saved["model_providers"]["Databricks"]["base_url"].endswith("/ai-gateway/codex/v1")
    assert saved["model_providers"]["Databricks"]["auth"]["args"][-1] == "ws"
    assert saved["notify"] == ["terminal-notifier"]
    assert saved["model_providers"]["Other"]["name"] == "Other"


def test_opencode_desktop_registers_only_managed_deepseek_and_keeps_user_settings(
    monkeypatch, tmp_path
):
    path = tmp_path / "opencode" / "opencode.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "theme": "my-theme",
                "provider": {
                    "unrelated": {"npm": "other"},
                    "databricks-oss": {"models": {"system.ai.deepseek-v4-1-flash": {}}},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop_config, "OPENCODE_DESKTOP_CONFIG_PATH", path)
    monkeypatch.setattr(
        desktop_config, "OPENCODE_DESKTOP_BACKUP_PATH", tmp_path / "opencode.backup"
    )
    monkeypatch.setattr(desktop_config, "OPENCODE_PLUGIN_BACKUP_PATH", tmp_path / "plugin.backup")
    monkeypatch.setattr(desktop_config, "get_databricks_token", lambda *a: "test-token")
    model = "platform_ai_gateway_dbricks.models.azure-deepseek-v4-1-flash"

    desktop_config.sync_opencode_desktop(
        {
            "workspace": WORKSPACE,
            "profile": "ws",
            "opencode_default_model": model,
            "opencode_models": {"oss": [model]},
        }
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["model"] == f"databricks-oss/{model}"
    assert list(saved["provider"]["databricks-oss"]["models"]) == [model]
    assert saved["provider"]["unrelated"] == {"npm": "other"}
    assert saved["theme"] == "my-theme"
    assert (path.parent / "plugin" / "ucode-auth.js").exists()


def test_revert_desktop_restores_existing_configs_and_removes_created_plugin(monkeypatch, tmp_path):
    codex_path = tmp_path / "codex" / "config.toml"
    codex_path.parent.mkdir()
    codex_path.write_text('model = "original"\n', encoding="utf-8")
    opencode_path = tmp_path / "opencode" / "opencode.json"
    opencode_path.parent.mkdir()
    opencode_path.write_text('{"theme": "original"}', encoding="utf-8")
    monkeypatch.setattr(codex, "_legacy_config_path", lambda: codex_path)
    monkeypatch.setattr(desktop_config, "OPENCODE_DESKTOP_CONFIG_PATH", opencode_path)
    monkeypatch.setattr(desktop_config, "CODEX_DESKTOP_BACKUP_PATH", tmp_path / "codex.backup")
    monkeypatch.setattr(
        desktop_config, "OPENCODE_DESKTOP_BACKUP_PATH", tmp_path / "opencode.backup"
    )
    monkeypatch.setattr(desktop_config, "OPENCODE_PLUGIN_BACKUP_PATH", tmp_path / "plugin.backup")

    desktop_config.backup_desktop_config("codex")
    desktop_config.backup_desktop_config("opencode")
    codex_path.write_text('model = "managed"\n', encoding="utf-8")
    opencode_path.write_text('{"model": "managed"}', encoding="utf-8")
    plugin = opencode_path.parent / "plugin" / "ucode-auth.js"
    plugin.parent.mkdir()
    plugin.write_text("managed", encoding="utf-8")
    results = desktop_config.revert_desktop_config()

    assert results == {"codex": True, "opencode": True, "opencode_plugin": True}
    assert codex_path.read_text(encoding="utf-8") == 'model = "original"\n'
    assert opencode_path.read_text(encoding="utf-8") == '{"theme": "original"}'
    assert not plugin.exists()


def test_opencode_desktop_refuses_unreadable_settings(monkeypatch, tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text("{ // user comments\n }", encoding="utf-8")
    monkeypatch.setattr(desktop_config, "OPENCODE_DESKTOP_CONFIG_PATH", path)
    monkeypatch.setattr(desktop_config, "OPENCODE_DESKTOP_BACKUP_PATH", tmp_path / "config.backup")
    monkeypatch.setattr(desktop_config, "OPENCODE_PLUGIN_BACKUP_PATH", tmp_path / "plugin.backup")
    monkeypatch.setattr(desktop_config, "get_databricks_token", lambda *a: "test-token")
    model = "platform_ai_gateway_dbricks.models.azure-deepseek-v4-1-flash"

    import pytest

    with pytest.raises(RuntimeError, match="Cannot update OpenCode Desktop settings"):
        desktop_config.sync_opencode_desktop(
            {
                "workspace": WORKSPACE,
                "opencode_default_model": model,
                "opencode_models": {"oss": [model]},
            }
        )
    assert path.read_text(encoding="utf-8") == "{ // user comments\n }"
