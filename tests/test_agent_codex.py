"""Tests for agents/codex.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import tomlkit

from ucode import managed_files
from ucode.agents import LaunchOptions, codex
from ucode.config_io import read_toml_safe
from ucode.smart_routing import codex_routing

WS = "https://example.databricks.com"


class TestCodexSpec:
    def test_binary(self):
        assert codex.SPEC["binary"] == "codex"

    def test_package(self):
        assert codex.SPEC["package"] == "@openai/codex"

    def test_display(self):
        assert codex.SPEC["display"] == "Codex"


class TestMinimumVersion:
    @pytest.mark.parametrize("version", ["0.145.0", "0.148.0", "1.0.0"])
    def test_supported_version(self, monkeypatch, version):
        monkeypatch.setattr(codex, "agent_version", lambda _binary: version)

        assert codex.minimum_version_error() is None

    def test_older_version_requires_update(self, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda _binary: "0.144.0")

        expected = "ug requires Codex 0.145.0 or newer; found 0.144.0."
        assert codex.minimum_version_error() == expected

    def test_unknown_version_does_not_block(self, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda _binary: "unknown")

        assert codex.minimum_version_error() is None


class TestHasUcodeConfig:
    def test_detects_profile_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "ucode.config.toml"
        legacy_path = tmp_path / "config.toml"
        legacy_path.write_text(
            'profile = "ucode"\n\n[profiles.ucode]\nmodel_provider = "ucode-databricks"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)

        assert codex.has_ucode_config() is True

    def test_ignores_unrelated_legacy_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "ucode.config.toml"
        legacy_path = tmp_path / "config.toml"
        legacy_path.write_text('profile = "default"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)

        assert codex.has_ucode_config() is False


class TestRenderOverlay:
    def test_uses_profile_file_shape_without_legacy_profiles(self):
        overlay = codex.render_overlay(WS)
        assert "profile" not in overlay
        assert "profiles" not in overlay

    def test_sets_model_provider(self):
        overlay = codex.render_overlay(WS)
        assert overlay["model_provider"] == "Databricks"

    def test_sets_model_when_provided(self):
        overlay = codex.render_overlay(WS, "databricks-gpt-5")
        assert overlay["model"] == "databricks-gpt-5"

    def test_provider_base_url(self):
        overlay = codex.render_overlay(WS)
        provider = overlay["model_providers"]["Databricks"]
        assert provider["base_url"] == f"{WS}/ai-gateway/codex/v1"

    def test_provider_wire_api(self):
        overlay = codex.render_overlay(WS)
        provider = overlay["model_providers"]["Databricks"]
        assert provider["wire_api"] == "responses"

    def test_auth_runs_ug_auth_token(self, monkeypatch):
        # The auth command runs the `ug auth-token` executable directly
        # (not `sh -c`), so it works on Windows where there is no POSIX shell.
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/tools/{command}")
        overlay = codex.render_overlay(WS)
        auth = overlay["model_providers"]["Databricks"]["auth"]
        assert auth["command"] == "/tools/ug"
        assert auth["args"][0] == "auth-token"
        assert auth["command"] != "sh"

    def test_auth_contains_workspace(self):
        overlay = codex.render_overlay(WS)
        auth = overlay["model_providers"]["Databricks"]["auth"]
        assert any(WS in arg for arg in auth["args"])

    def test_auth_uses_custom_oauth_options(self):
        overlay = codex.render_overlay(
            WS,
            custom_oauth={
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
            },
        )
        auth = overlay["model_providers"]["Databricks"]["auth"]
        assert auth["args"] == [
            "auth-token",
            "--host",
            WS,
            "--client-id",
            "custom-client",
            "--redirect-url",
            "http://localhost:8020/callback",
            "--scopes",
            "offline_access,model-serving",
        ]

    def test_auth_refresh_interval(self):
        overlay = codex.render_overlay(WS)
        auth = overlay["model_providers"]["Databricks"]["auth"]
        assert auth["refresh_interval_ms"] == 900_000

    @pytest.mark.parametrize("render", [codex.render_overlay, codex.render_legacy_overlay])
    @pytest.mark.parametrize("custom", [False, True])
    def test_auth_timeout_allows_custom_browser_login_only(self, render, custom):
        config = (
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
            }
            if custom
            else None
        )
        overlay = render(WS, custom_oauth=config)
        auth = overlay["model_providers"][codex.CODEX_MODEL_PROVIDER_NAME]["auth"]
        assert auth["timeout_ms"] == (180_000 if custom else 5000)

    def test_provider_adds_routing_header(self):
        overlay = codex.render_overlay(WS, provider="main.aarushi.aarushi-openai")
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
        assert headers["Databricks-Model-Provider-Service"] == "main.aarushi.aarushi-openai"

    def test_provider_omits_model(self):
        overlay = codex.render_overlay(WS, model=None, provider="main.aarushi.aarushi-openai")
        assert "model" not in overlay

    def test_no_provider_header_without_flag(self):
        overlay = codex.render_overlay(WS)
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
        assert "Databricks-Model-Provider-Service" not in headers

    def test_parent_adds_discovery_header(self):
        overlay = codex.render_overlay(WS, parent_schema="main.default")
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
        assert headers["Databricks-Model-Service-Parent-Schema"] == "main.default"

    def test_managed_http_headers_added(self):
        overlay = codex.render_overlay(
            WS, managed_http_headers={"x-databricks-workspace": "eng-ml-inference"}
        )
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
        assert headers["x-databricks-workspace"] == "eng-ml-inference"
        assert "User-Agent" in headers  # ucode's own header is retained alongside.

    def test_managed_http_headers_override_ucode_header(self, monkeypatch):
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.123.0")
        overlay = codex.render_overlay(
            WS,
            provider="main.x.svc",
            managed_http_headers={"databricks-model-provider-service": "admin.override"},
        )
        headers = overlay["model_providers"]["Databricks"]["http_headers"]
        # Admin wins on a case-insensitive collision, leaving no duplicate spelling of the header.
        assert headers == {
            "User-Agent": "ucode/0.1.0 codex/0.123.0",
            "databricks-model-provider-service": "admin.override",
        }


class TestRenderOverlayUserAgent:
    def test_user_agent_set_on_provider(self, monkeypatch):
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.123.0")
        overlay = codex.render_overlay(WS)
        provider = overlay["model_providers"]["Databricks"]
        assert provider["http_headers"]["User-Agent"] == "ucode/0.1.0 codex/0.123.0"

    def test_managed_keys_include_http_headers(self):
        # Revert must clean up the new key.
        assert ["model_providers", "Databricks", "http_headers"] in codex.MANAGED_KEYS
        assert ["model_catalog_json"] not in codex.MANAGED_KEYS


class TestCodexWriteConfig:
    def test_writes_ucode_profile_config_file(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        doc = read_toml_safe(config_path)
        assert doc["model_provider"] == "Databricks"
        assert "model" not in doc
        assert "model_reasoning_effort" not in doc
        assert "profiles" not in doc

    def test_smart_routing_preserves_configured_startup_model(self, tmp_path, monkeypatch):
        config_path = tmp_path / "ucode.config.toml"
        config_path.write_text('model = "gpt-5.6-sol"\n')
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.145.0")
        monkeypatch.setenv(codex.smart_routing_v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.delenv("CODEX_HOME", raising=False)
        state = {"workspace": WS}

        assert codex.default_model(state) == "gpt-5.6-sol"
        codex.write_tool_config(state)

        assert read_toml_safe(config_path)["model"] == "gpt-5.6-sol"
        assert codex._smart_routing_config_model(state) == "gpt-5.6-sol"

    def test_removes_discovered_model_id(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config(
            {"workspace": WS, "codex_models": ["databricks-gpt-5", "databricks-gpt-5-5"]}
        )

        doc = read_toml_safe(config_path)
        assert "model" not in doc

    def test_removes_uc_model_services_id(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config(
            {"workspace": WS, "codex_models": ["system.ai.gpt-5", "system.ai.gpt-5-5"]}
        )

        doc = read_toml_safe(config_path)
        assert "model" not in doc

    def test_smart_routing_prunes_stale_catalog_reference(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('model_catalog_json = "/tmp/stale.json"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", tmp_path / "catalog.json")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        monkeypatch.setenv(codex.smart_routing_v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        # Smart routing selects dynamically, so a leftover static catalog is not kept.
        assert "model_catalog_json" not in read_toml_safe(config_path)

    def test_unmanaged_configure_prunes_catalog_reference(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('model_catalog_json = "/tmp/prior.json"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", tmp_path / "catalog.json")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        # An unmanaged configure builds no catalog, so a prior config's catalog reference is dropped.
        assert "model_catalog_json" not in read_toml_safe(config_path)

    def test_provider_drops_stale_model_without_persisting_header(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        # An earlier run pinned a model.
        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})
        assert "model" not in read_toml_safe(config_path)

        # The routing header is added only to the launch config.
        codex.write_tool_config(
            {"workspace": WS, "codex_models": ["gpt-5"]},
            provider="main.aarushi.aarushi-openai",
        )

        doc = read_toml_safe(config_path)
        assert "model" not in doc
        headers = doc["model_providers"]["Databricks"]["http_headers"]
        assert "Databricks-Model-Provider-Service" not in headers

    def test_non_provider_write_removes_stale_provider_header(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state, provider="main.default.openai")
        codex.write_tool_config(state)

        headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        assert "Databricks-Model-Provider-Service" not in headers

    def test_replaces_stale_routing_headers(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        state = {"workspace": WS, "codex_models": []}

        codex.write_tool_config(state, provider="main.default.openai")
        codex.write_tool_config(state, parent_schema="main.default")

        headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        assert headers["Databricks-Model-Service-Parent-Schema"] == "main.default"
        assert "Databricks-Model-Provider-Service" not in headers

        codex.write_tool_config(state)

        headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        assert "Databricks-Model-Service-Parent-Schema" not in headers
        assert "Databricks-Model-Provider-Service" not in headers

    def test_writes_admin_http_headers(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        state = {
            "workspace": WS,
            "codex_models": [],
            "codex_http_headers": {"x-databricks-workspace": "eng-ml-inference"},
        }

        codex.write_tool_config(state)

        headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        assert headers["x-databricks-workspace"] == "eng-ml-inference"  # admin header applied
        assert "User-Agent" in headers  # ucode's own header kept

    def test_drops_admin_http_header_after_removal(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config(
            {
                "workspace": WS,
                "codex_models": [],
                "codex_http_headers": {"x-databricks-workspace": "eng-ml-inference"},
            }
        )
        # The admin removes the header from managed config; the next configure omits it entirely.
        codex.write_tool_config({"workspace": WS, "codex_models": []})

        headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        assert "x-databricks-workspace" not in headers  # dropped on removal
        assert "User-Agent" in headers

    def test_legacy_replaces_stale_routing_headers(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        legacy_path = config_dir / "config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_dir / "ucode.config.toml")
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_BACKUP_PATH", tmp_path / "legacy-backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.133.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        state = {"workspace": WS, "codex_models": []}

        codex.write_tool_config(state, provider="main.default.openai")
        codex.write_tool_config(state, parent_schema="main.default")

        headers = read_toml_safe(legacy_path)["model_providers"]["Databricks"]["http_headers"]
        assert headers["Databricks-Model-Service-Parent-Schema"] == "main.default"
        assert "Databricks-Model-Provider-Service" not in headers

        codex.write_tool_config(state)
        headers = read_toml_safe(legacy_path)["model_providers"]["Databricks"]["http_headers"]
        assert "Databricks-Model-Service-Parent-Schema" not in headers
        assert "Databricks-Model-Provider-Service" not in headers

    def test_clears_profile_model_preferences_before_launch(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            'model = "system.ai.gpt-5-6-luna"\nmodel_reasoning_effort = "medium"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")

        assert codex.clear_model_preferences({}) is True

        doc = read_toml_safe(config_path)
        assert "model" not in doc
        assert "model_reasoning_effort" not in doc

    def test_preserves_profile_without_model_preferences(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir()
        config_path.write_text('model_provider = "ucode-databricks"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")

        assert codex.clear_model_preferences({}) is False
        assert not (tmp_path / "backup.toml").exists()

    def test_preserves_profile_model_for_managed_default(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir()
        config_path.write_text('model = "managed-default"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)

        assert codex.clear_model_preferences({"codex_default_model": "managed-default"}) is False
        assert read_toml_safe(config_path)["model"] == "managed-default"

    def test_removes_legacy_ucode_profile_from_shared_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        profile_path = config_dir / "ucode.config.toml"
        legacy_path = config_dir / "config.toml"
        legacy_path.write_text(
            'profile = "ucode"\n\n'
            "[profiles.ucode]\n"
            'model_provider = "old"\n\n'
            "[profiles.other]\n"
            'model_provider = "keep"\n',
            encoding="utf-8",
        )
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        legacy_backup_path = tmp_path / "codex-legacy-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        doc = read_toml_safe(legacy_path)
        assert "profile" not in doc
        assert "ucode" not in doc["profiles"]
        assert doc["profiles"]["other"]["model_provider"] == "keep"
        assert legacy_backup_path.exists()

    def test_writes_legacy_shared_config_when_codex_too_old(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        legacy_path = config_dir / "config.toml"
        profile_path = config_dir / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        legacy_backup_path = tmp_path / "codex-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_BACKUP_PATH", legacy_backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.133.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        # Per-profile file must not be written for old Codex.
        assert not profile_path.exists()
        doc = read_toml_safe(legacy_path)
        assert doc["profile"] == "ucode"
        assert doc["profiles"]["ucode"]["model_provider"] == "Databricks"
        assert "model" not in doc["profiles"]["ucode"]
        provider = doc["model_providers"]["Databricks"]
        assert provider["base_url"] == f"{WS}/ai-gateway/codex/v1"
        assert provider["wire_api"] == "responses"

    def test_config_write_does_not_persist_smart_routing_hooks(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            "[[hooks.PreToolUse]]\n"
            'matcher = "Bash"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "user-policy"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.145.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config(
            {
                "workspace": WS,
                "profile": "prod",
                "codex_models": ["databricks-gpt-5", "databricks-gpt-5-5"],
                "oss_models": ["system.ai.glm-5-2"],
                codex.SMART_ROUTING_STATE_KEY: True,
            }
        )

        doc = read_toml_safe(config_path)
        assert set(doc["hooks"]) == {"PreToolUse"}
        pre_tool_commands = [
            hook["command"] for group in doc["hooks"]["PreToolUse"] for hook in group["hooks"]
        ]
        assert pre_tool_commands == ["user-policy"]

    def test_config_write_removes_legacy_routing_hooks(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        backup_path = tmp_path / "backup.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            "[[hooks.PreToolUse]]\n"
            'matcher = "Agent"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "ucode codex-router-hook route-subagent"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.145.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        state = {
            "workspace": WS,
            "codex_models": ["databricks-gpt-5"],
            codex.SMART_ROUTING_STATE_KEY: True,
        }

        codex.write_tool_config(state)

        assert "hooks" not in read_toml_safe(config_path)

    def test_legacy_write_preserves_other_profiles_in_shared_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        legacy_path = config_dir / "config.toml"
        legacy_path.write_text(
            '[profiles.other]\nmodel_provider = "keep"\n',
            encoding="utf-8",
        )
        profile_path = config_dir / "ucode.config.toml"
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        legacy_backup_path = tmp_path / "codex-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_BACKUP_PATH", legacy_backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.133.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        doc = read_toml_safe(legacy_path)
        assert doc["profiles"]["other"]["model_provider"] == "keep"
        assert doc["profiles"]["ucode"]["model_provider"] == "Databricks"


class TestCodexLegacyLayoutDetection:
    def test_new_codex_uses_modern_layout(self, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")

        assert codex._use_legacy_layout() is False

    def test_old_codex_uses_legacy_layout(self, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.133.0")

        assert codex._use_legacy_layout() is True

    def test_unknown_version_uses_modern_layout(self, monkeypatch):
        monkeypatch.setattr(codex, "agent_version", lambda binary: "unknown")

        assert codex._use_legacy_layout() is False


class TestCodexSmartRouting:
    def test_disable_removes_only_ucode_hooks(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        legacy_path = tmp_path / ".codex" / "config.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            "[[hooks.PreToolUse]]\n"
            'matcher = "Bash"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "user-policy"\n\n'
            "[[hooks.PreToolUse]]\n"
            'matcher = "Agent"\n'
            "[[hooks.PreToolUse.hooks]]\n"
            'type = "command"\n'
            'command = "ucode codex-router-hook route-subagent"\n\n'
            "[[hooks.SessionStart]]\n"
            "[[hooks.SessionStart.hooks]]\n"
            'type = "command"\n'
            'command = "ucode codex-router-hook session-start"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        monkeypatch.setattr(codex_routing, "clear_routing_artifacts", lambda: None)
        state = {"workspace": WS, codex.SMART_ROUTING_STATE_KEY: True}

        assert codex.disable_smart_routing(state) is True

        doc = read_toml_safe(config_path)
        assert state.get(codex.SMART_ROUTING_STATE_KEY) is None
        assert list(doc["hooks"]) == ["PreToolUse"]
        assert doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "user-policy"


class TestCodexRemoveLegacyProfile:
    @pytest.mark.parametrize("old_ucode_entries", [False, True])
    def test_preserves_users_databricks_provider(self, tmp_path, monkeypatch, old_ucode_entries):
        profile_path = tmp_path / "ucode.config.toml"
        shared_path = tmp_path / "config.toml"
        original = (
            'model_provider = "Databricks"\n'
            "[model_providers.Databricks]\n"
            'name = "User gateway"\n'
            'base_url = "https://user.example.com"\n'
        )
        if old_ucode_entries:
            original += (
                '[profiles.ucode]\nmodel_provider = "ucode-databricks"\n'
                '[model_providers.ucode-databricks]\nname = "Old ug gateway"\n'
            )
        shared_path.write_text(original)
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.154.0")
        monkeypatch.setattr(codex, "save_state", lambda _: None)

        codex.write_tool_config({"workspace": WS})

        shared = read_toml_safe(shared_path)
        assert shared["model_provider"] == "Databricks"
        assert shared["model_providers"]["Databricks"] == {
            "name": "User gateway",
            "base_url": "https://user.example.com",
        }
        assert "ucode-databricks" not in shared["model_providers"]
        assert read_toml_safe(profile_path)["model_provider"] == "Databricks"
        if not old_ucode_entries:
            assert shared_path.read_text() == original

    def test_drops_provider_block_on_modern_path(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        profile_path = config_dir / "ucode.config.toml"
        legacy_path = config_dir / "config.toml"
        legacy_path.write_text(
            'profile = "ucode"\n\n'
            "[profiles.ucode]\n"
            'model_provider = "ucode-databricks"\n\n'
            "[model_providers.ucode-databricks]\n"
            'name = "Databricks AI Gateway"\n\n'
            "[model_providers.other]\n"
            'name = "keep"\n',
            encoding="utf-8",
        )
        backup_path = tmp_path / "codex-ucode-config.backup.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", backup_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        doc = read_toml_safe(legacy_path)
        assert "profile" not in doc
        assert "ucode" not in doc.get("profiles", {})
        assert "ucode-databricks" not in doc["model_providers"]
        assert doc["model_providers"]["other"]["name"] == "keep"


class TestCodexRevertLegacySharedConfig:
    def test_strips_all_ucode_entries(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        profile_path = config_dir / "ucode.config.toml"
        legacy_path = config_dir / "config.toml"
        legacy_path.write_text(
            'profile = "ucode"\n\n'
            "[profiles.ucode]\n"
            'model_provider = "ucode-databricks"\n\n'
            "[profiles.other]\n"
            'model_provider = "keep"\n\n'
            "[model_providers.ucode-databricks]\n"
            'name = "Databricks AI Gateway"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)

        assert codex.revert_legacy_shared_config() is True

        doc = read_toml_safe(legacy_path)
        assert "profile" not in doc
        assert "ucode" not in doc["profiles"]
        assert doc["profiles"]["other"]["model_provider"] == "keep"
        assert "model_providers" not in doc

    def test_returns_false_when_no_ucode_entries(self, tmp_path, monkeypatch):
        config_dir = tmp_path / ".codex"
        config_dir.mkdir()
        profile_path = config_dir / "ucode.config.toml"
        legacy_path = config_dir / "config.toml"
        legacy_path.write_text('[profiles.other]\nmodel_provider = "keep"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)

        assert codex.revert_legacy_shared_config() is False

        doc = read_toml_safe(legacy_path)
        assert doc["profiles"]["other"]["model_provider"] == "keep"

    def test_returns_false_when_no_shared_config(self, tmp_path, monkeypatch):
        profile_path = tmp_path / ".codex" / "ucode.config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)

        assert codex.revert_legacy_shared_config() is False

    def test_strips_ucode_app_catalog_reference(self, capsys):
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        shared_path.parent.mkdir()
        catalog_path = codex.CODEX_MODEL_CATALOG_PATH
        catalog_path.write_text("{}", encoding="utf-8")
        shared_path.write_text(
            f'model_catalog_json = "{catalog_path}"\npersonality = "friendly"\n',
            encoding="utf-8",
        )
        assert codex.revert_legacy_shared_config() is True

        assert read_toml_safe(shared_path) == {"personality": "friendly"}
        assert not catalog_path.exists()
        assert "codex app-server daemon restart" in " ".join(capsys.readouterr().err.split())


class TestCodexAppCatalog:
    def test_publish_refresh_and_reattach_preserve_settings_and_report_restart(self, capsys):
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        shared_path.parent.mkdir()
        original = '# User settings\nmodel = "gpt-user"\n'
        shared_path.write_text(original, encoding="utf-8")
        first_catalog = {"models": [{"slug": "first-model"}]}
        second_catalog = {"models": [{"slug": "second-model"}]}

        codex.sync_app_model_catalog(first_catalog)
        first_config = shared_path.read_text()
        assert "# User settings" in first_config
        assert read_toml_safe(shared_path) == {
            "model": "gpt-user",
            "model_catalog_json": str(codex.CODEX_MODEL_CATALOG_PATH),
        }
        assert json.loads(codex.CODEX_MODEL_CATALOG_PATH.read_text()) == first_catalog
        published = capsys.readouterr()
        assert published.out == ""
        assert "codex app-server daemon restart" in " ".join(published.err.split())
        assert "After active tasks finish" in published.err

        codex.sync_app_model_catalog(first_catalog)
        unchanged = capsys.readouterr()
        assert unchanged.out == unchanged.err == ""

        codex.sync_app_model_catalog(second_catalog)
        assert shared_path.read_text() == first_config
        assert json.loads(codex.CODEX_MODEL_CATALOG_PATH.read_text()) == second_catalog
        refreshed = capsys.readouterr()
        assert refreshed.out == ""
        assert "codex app-server daemon restart" in " ".join(refreshed.err.split())

        # Reattaching an unchanged catalog also requires a server restart.
        shared_path.write_text(original, encoding="utf-8")
        codex.sync_app_model_catalog(second_catalog)
        assert shared_path.read_text() == first_config
        assert "codex app-server daemon restart" in " ".join(capsys.readouterr().err.split())

    def test_invalid_shared_config_is_not_overwritten(self):
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        shared_path.parent.mkdir()
        original = 'model = "unfinished\n'
        shared_path.write_text(original, encoding="utf-8")
        codex.CODEX_MODEL_CATALOG_PATH.write_text("previous catalog", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Cannot update Codex App settings"):
            codex.sync_app_model_catalog({"models": [{"slug": "gpt-mps"}]})

        assert shared_path.read_text() == original
        assert codex.CODEX_MODEL_CATALOG_PATH.read_text() == "previous catalog"

    @pytest.mark.parametrize("previous_catalog", [False, True])
    def test_custom_provider_keeps_its_own_model_discovery(self, previous_catalog, capsys):
        if previous_catalog:
            codex.sync_app_model_catalog({"models": [{"slug": "previous"}]})
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        shared_path.parent.mkdir(exist_ok=True)
        original = '# User settings\nmodel_provider = "custom"\nmodel = "user-model"\n'
        shared_path.write_text(
            original
            + (
                f'model_catalog_json = "{codex.CODEX_MODEL_CATALOG_PATH}"\n'
                if previous_catalog
                else ""
            ),
            encoding="utf-8",
        )

        capsys.readouterr()
        codex.sync_app_model_catalog({"models": [{"slug": "gpt-mps"}]})

        assert shared_path.read_text() == original
        output = capsys.readouterr().err
        if previous_catalog:
            assert "daemon restart" in " ".join(output.split())
        else:
            assert "daemon restart" not in output

    def test_dry_run_leaves_config_and_catalog_unchanged(self, monkeypatch):
        codex.sync_app_model_catalog({"models": [{"slug": "previous"}]})
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        original = shared_path.read_bytes()
        catalog_before = codex.CODEX_MODEL_CATALOG_PATH.read_bytes()
        monkeypatch.setattr(codex, "is_dry_run", lambda: True)

        codex.sync_app_model_catalog({"models": [{"slug": "new"}]})

        assert shared_path.read_bytes() == original
        assert codex.CODEX_MODEL_CATALOG_PATH.read_bytes() == catalog_before

    def test_reconfigure_clears_discovered_app_catalog(self, monkeypatch):
        codex.sync_app_model_catalog({"models": [{"slug": "previous-workspace"}]})
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.154.0")
        monkeypatch.setattr(codex, "save_state", lambda _: None)

        codex.write_tool_config({"workspace": WS})

        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        assert "model_catalog_json" not in read_toml_safe(shared_path)
        assert not codex.CODEX_MODEL_CATALOG_PATH.exists()

    def test_failed_static_validation_detaches_previous_app_catalog(self, monkeypatch):
        codex.sync_app_model_catalog({"models": [{"slug": "old-model"}]})
        original_catalog = codex.CODEX_MODEL_CATALOG_PATH.read_bytes()
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.154.0")

        def reject(*args):
            raise RuntimeError("catalog is incompatible")

        monkeypatch.setattr(codex, "prepare_codex_catalog", reject)
        with pytest.raises(RuntimeError, match="incompatible"):
            codex.write_tool_config({"workspace": WS, "codex_static_models": ["new-model"]})

        assert "model_catalog_json" not in read_toml_safe(
            codex.CODEX_CONFIG_PATH.parent / "config.toml"
        )
        assert codex.CODEX_MODEL_CATALOG_PATH.read_bytes() == original_catalog

    def test_revert_preserves_subsequent_user_catalog(self, tmp_path):
        codex.sync_app_model_catalog({"models": [{"slug": "gpt-mps"}]})
        shared_path = codex.CODEX_CONFIG_PATH.parent / "config.toml"
        original = 'model_catalog_json = "/user/models.json"\n'
        shared_path.write_text(original, encoding="utf-8")

        assert codex.revert_legacy_shared_config() is False
        assert shared_path.read_text() == original


class TestCodexDefaultModel:
    @pytest.fixture(autouse=True)
    def _isolate_profile_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", tmp_path / "ucode.config.toml")
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")

    def test_reading_default_model_preserves_profile_preferences(self, tmp_path):
        codex.CODEX_CONFIG_PATH.write_text(
            'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "medium"\n', encoding="utf-8"
        )

        assert codex.default_model({"codex_models": ["system.ai.gpt-5-6-luna"]}) is None
        doc = read_toml_safe(codex.CODEX_CONFIG_PATH)
        assert doc["model"] == "gpt-5.6-sol"
        assert doc["model_reasoning_effort"] == "medium"

    def test_none_when_no_configured_model(self):
        assert codex.default_model({}) is None

    def test_managed_default_model_takes_priority(self):
        state = {
            "codex_default_model": "admin-chosen-default",
            "codex_models": ["databricks-gpt-5-5"],
        }
        assert codex.default_model(state) == "admin-chosen-default"


class TestCodexValidateCmd:
    def test_starts_with_binary(self):
        cmd = codex.validate_cmd("codex")
        assert cmd[0] == "codex"

    def test_uses_exec_subcommand(self):
        cmd = codex.validate_cmd("codex")
        assert "exec" in cmd

    def test_uses_ucode_profile(self):
        cmd = codex.validate_cmd("codex")
        assert cmd[:3] == ["codex", "--profile", "ucode"]

    def test_has_prompt(self):
        cmd = codex.validate_cmd("codex")
        assert len(cmd) > 2

    def test_skips_git_repo_check(self):
        # Validation runs in arbitrary cwd (e.g., ~/Documents); without this
        # flag Codex refuses to run outside a trusted/git directory.
        cmd = codex.validate_cmd("codex")
        assert "--skip-git-repo-check" in cmd


class TestCodexLaunch:
    """Launches use the configuration layout supported by the installed Codex."""

    @staticmethod
    def _patch(tmp_path, monkeypatch):
        profile_path = tmp_path / "ucode.config.toml"
        profile_path.write_text(
            'model_provider = "Databricks"\n\n'
            "[model_providers.Databricks]\n"
            'name = "Databricks AI Gateway"\n'
            'base_url = "https://example.databricks.com/ai-gateway/codex/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        launches: list[list[str]] = []
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "exec_or_spawn", lambda argv, *, env: launches.append(argv))
        monkeypatch.setattr(
            codex,
            "get_databricks_token",
            lambda workspace, profile=None, force_refresh=False: "tok",
        )
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)
        # These launch tests isolate the real-binary validation boundary; its
        # subprocess contract is covered in test_codex_catalog.py.
        monkeypatch.setattr(codex, "validate_codex_catalog", lambda binary, catalog: None)
        return launches

    def test_sets_oauth_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OAUTH_TOKEN", "stale-parent-token")
        self._patch(tmp_path, monkeypatch)
        execute = Mock()
        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        monkeypatch.setattr(
            codex,
            "get_databricks_token",
            lambda workspace, profile=None, force_refresh=False: "fresh-token",
        )
        codex.launch({"workspace": WS}, ["--search"], options=LaunchOptions())

        assert execute.call_args.kwargs["env"]["OAUTH_TOKEN"] == "fresh-token"
        assert os.environ["OAUTH_TOKEN"] == "stale-parent-token"
        assert execute.call_args.args[0][-1] == "--search"

    @pytest.mark.parametrize("launch_path", ["normal", "legacy", "prepared-catalog"])
    @pytest.mark.parametrize("use_pat", [False, True])
    def test_custom_env_reaches_agent_after_auth_and_preparation(
        self, tmp_path, monkeypatch, launch_path, use_pat
    ):
        self._patch(tmp_path, monkeypatch)
        execute = Mock()
        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        monkeypatch.setenv("UG_TEST_CUSTOM", "parent")
        monkeypatch.setenv("UG_TEST_UNRELATED", "keep")
        monkeypatch.setenv("OAUTH_TOKEN", "stale-parent-token")
        parent_path = os.environ.get("PATH")

        def token(workspace, profile=None, force_refresh=False):
            assert workspace == WS
            assert os.environ["UG_TEST_CUSTOM"] == "parent"
            assert os.environ.get("PATH") == parent_path
            return "fresh-pat" if use_pat else "fresh-oauth"

        def version(binary):
            assert os.environ["UG_TEST_CUSTOM"] == "parent"
            assert os.environ.get("PATH") == parent_path
            return "0.133.0" if launch_path == "legacy" else "0.154.0"

        monkeypatch.setattr(codex, "get_databricks_token", token)
        monkeypatch.setattr(codex, "agent_version", version)
        custom = {"UG_TEST_CUSTOM": "  line one\nline two  ", "UG_TEST_EMPTY": "", "PATH": ""}
        state = {"workspace": WS, "codex_custom_env": custom, "use_pat": use_pat}
        if launch_path == "prepared-catalog":
            state["_codex_launch_provider"] = "main.schema.provider"
            state["_codex_prepared_catalog"] = {
                "provider": "main.schema.provider",
                "parent_schema": None,
                "catalog": {"models": [{"slug": "prepared-model"}]},
                "catalog_path": str(tmp_path / "catalog.json"),
            }
            monkeypatch.setattr(
                codex, "_fetch_codex_model_catalog", Mock(side_effect=AssertionError("late fetch"))
            )

        codex.launch(state, ["exec", "prompt"], options=LaunchOptions())

        child = execute.call_args.kwargs["env"]
        assert {key: child[key] for key in custom} == custom
        assert child["UG_TEST_UNRELATED"] == "keep"
        assert child["OAUTH_TOKEN"] == ("fresh-pat" if use_pat else "fresh-oauth")
        assert os.environ["OAUTH_TOKEN"] == "stale-parent-token"
        assert os.environ["UG_TEST_CUSTOM"] == "parent"
        assert os.environ.get("PATH") == parent_path
        argv = execute.call_args.args[0]
        assert argv[-2:] == ["exec", "prompt"]
        if launch_path == "legacy":
            assert argv[:3] == ["codex", "--profile", "ucode"]
        elif launch_path == "prepared-catalog":
            assert 'model="prepared-model"' in argv

    @pytest.mark.parametrize("custom_catalog", [None, "/user/isaac-app-model-catalog.json"])
    def test_native_update_detaches_catalog_without_discovery(
        self, tmp_path, monkeypatch, custom_catalog
    ):
        self._patch(tmp_path, monkeypatch)
        codex.sync_app_model_catalog({"models": [{"slug": "old-model"}]})
        shared_path = tmp_path / "config.toml"
        if custom_catalog:
            shared_path.write_text(f'model_catalog_json = "{custom_catalog}"\n')
        profile_path = codex.CODEX_CONFIG_PATH
        profile_path.write_text(
            f'model_catalog_json = "{codex.CODEX_MODEL_CATALOG_PATH}"\n' + profile_path.read_text()
        )
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda *a, **k: pytest.fail("update must not depend on gateway discovery"),
        )
        monkeypatch.setattr(
            codex,
            "validate_codex_catalog",
            lambda *a, **k: pytest.fail("update must not load an old catalog"),
        )
        launches = []

        def execute(argv, *, env):
            assert read_toml_safe(shared_path).get("model_catalog_json") == custom_catalog
            assert not any(arg.startswith("model_catalog_json=") for arg in argv)
            launches.append(argv)

        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            ["update"],
            options=LaunchOptions(),
        )

        assert len(launches) == 1
        assert launches[0][-1] == "update"
        assert read_toml_safe(shared_path).get("model_catalog_json") == custom_catalog

    def test_provider_discovery_uses_authoritative_catalog(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        catalog_path = tmp_path / "models.json"
        app_catalog_path = tmp_path / "codex-model-catalog.json"
        catalog = {"models": [{"slug": "gpt-mps", "future_metadata": {"tools": True}}]}
        fetch_kwargs = {}
        validations = []
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", app_catalog_path)
        monkeypatch.setattr(codex, "_model_catalog_path", lambda workspace, provider: catalog_path)
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda workspace, token, **kwargs: fetch_kwargs.update(kwargs) or catalog,
        )

        def validate(binary, candidate):
            assert not catalog_path.exists()
            assert not app_catalog_path.exists()
            validations.append((binary, candidate))

        monkeypatch.setattr(codex, "validate_codex_catalog", validate)
        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            [],
            options=LaunchOptions(),
        )

        assert validations == [(codex.SPEC["binary"], catalog)]
        assert json.loads(catalog_path.read_text()) == catalog
        assert json.loads(app_catalog_path.read_text()) == catalog
        assert read_toml_safe(tmp_path / "config.toml")["model_catalog_json"] == str(
            app_catalog_path
        )
        assert f'model_catalog_json="{catalog_path}"' in launches[0]
        assert fetch_kwargs == {
            "source": codex.CodexCatalogSource.PROVIDER,
            "identifier": "main.default.openai",
        }
        # The MPS's primary target is pinned so the first request isn't a 403 on
        # Codex's bundled default, which the MPS allowlist doesn't route.
        assert 'model="gpt-mps"' in launches[0]
        provider_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert 'Databricks-Model-Provider-Service = "main.default.openai"' in provider_arg

    @pytest.mark.parametrize("custom_catalog", [None, "/user/isaac-app-model-catalog.json"])
    def test_incompatible_discovery_removes_only_ug_reference(
        self, tmp_path, monkeypatch, custom_catalog
    ):
        launches = self._patch(tmp_path, monkeypatch)
        codex.sync_app_model_catalog({"models": [{"slug": "old-model"}]})
        original_catalog = codex.CODEX_MODEL_CATALOG_PATH.read_bytes()
        shared_path = tmp_path / "config.toml"
        if custom_catalog:
            shared_path.write_text(f'model_catalog_json = "{custom_catalog}"\n')
        monkeypatch.setattr(
            codex, "_fetch_codex_model_catalog", lambda *a, **k: {"models": [{"slug": "new"}]}
        )

        def reject(binary, catalog):
            raise RuntimeError("The installed Codex cannot load this model catalog")

        monkeypatch.setattr(codex, "validate_codex_catalog", reject)

        with pytest.raises(RuntimeError, match="cannot load this model catalog"):
            codex.launch(
                {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
                [],
                options=LaunchOptions(),
            )

        assert not launches
        assert read_toml_safe(shared_path).get("model_catalog_json") == custom_catalog
        assert codex.CODEX_MODEL_CATALOG_PATH.read_bytes() == original_catalog
        assert not list(codex.CODEX_MODEL_CATALOG_PATH.parent.glob("codex-model-catalog-*.json"))

    def test_provider_discovery_preserves_custom_app_catalog(self, tmp_path, monkeypatch, capsys):
        launches = self._patch(tmp_path, monkeypatch)
        catalog_path = tmp_path / "models.json"
        app_catalog_path = tmp_path / "codex-model-catalog.json"
        shared_path = tmp_path / "config.toml"
        shared_path.write_text('model_catalog_json = "/user/models.json"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", app_catalog_path)
        monkeypatch.setattr(codex, "_model_catalog_path", lambda workspace, scope: catalog_path)
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda workspace, token, **kwargs: {"models": [{"slug": "gpt-mps"}]},
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            [],
            options=LaunchOptions(),
        )

        assert launches
        assert read_toml_safe(shared_path)["model_catalog_json"] == "/user/models.json"
        output = capsys.readouterr()
        assert "leaving it unchanged" in " ".join(output.err.split())
        assert "daemon restart" not in output.err

    def test_provider_pins_first_catalog_model(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        catalog = {"models": [{"slug": "gpt-primary"}, {"slug": "gpt-secondary"}]}
        monkeypatch.setattr(
            codex, "_model_catalog_path", lambda workspace, scope: tmp_path / "models.json"
        )
        monkeypatch.setattr(
            codex, "_fetch_codex_model_catalog", lambda workspace, token, **kwargs: catalog
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            [],
            options=LaunchOptions(),
        )

        assert 'model="gpt-primary"' in launches[0]

    @pytest.mark.parametrize("model_args", [["--model", "gpt-chosen"], ["-m", "gpt-chosen"]])
    def test_provider_does_not_override_user_model(self, tmp_path, monkeypatch, model_args):
        launches = self._patch(tmp_path, monkeypatch)
        catalog = {"models": [{"slug": "gpt-mps"}]}
        monkeypatch.setattr(
            codex, "_model_catalog_path", lambda workspace, scope: tmp_path / "models.json"
        )
        monkeypatch.setattr(
            codex, "_fetch_codex_model_catalog", lambda workspace, token, **kwargs: catalog
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            model_args,
            options=LaunchOptions(),
        )

        assert not any(arg.startswith("model=") for arg in launches[0])
        assert launches[0][-len(model_args) :] == model_args

    def test_provider_keeps_managed_default_model(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        # Top-level `model` (a managed default) must precede the table header so it
        # isn't parsed as a key inside [model_providers.ucode-databricks].
        profile_path = tmp_path / "ucode.config.toml"
        profile_path.write_text(
            'model_provider = "ucode-databricks"\n'
            'model = "managed-default"\n\n'
            "[model_providers.ucode-databricks]\n"
            'name = "Databricks AI Gateway"\n'
            'base_url = "https://example.databricks.com/ai-gateway/codex/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        catalog = {"models": [{"slug": "gpt-mps"}]}
        monkeypatch.setattr(
            codex, "_model_catalog_path", lambda workspace, scope: tmp_path / "models.json"
        )
        monkeypatch.setattr(
            codex, "_fetch_codex_model_catalog", lambda workspace, token, **kwargs: catalog
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            [],
            options=LaunchOptions(),
        )

        assert 'model="managed-default"' in launches[0]
        assert 'model="gpt-mps"' not in launches[0]

    def test_parent_discovery_uses_authoritative_catalog(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        catalog_path = tmp_path / "models.json"
        catalog = {"models": [{"slug": "gpt-parent"}]}
        fetch_kwargs = {}
        monkeypatch.setattr(codex, "_model_catalog_path", lambda workspace, scope: catalog_path)
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda workspace, token, **kwargs: fetch_kwargs.update(kwargs) or catalog,
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_parent_schema": "main.default"},
            [],
            options=LaunchOptions(),
        )

        assert catalog_path.exists()
        assert f'model_catalog_json="{catalog_path}"' in launches[0]
        assert fetch_kwargs == {
            "source": codex.CodexCatalogSource.PARENT_SCHEMA,
            "identifier": "main.default",
        }
        parent_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert 'Databricks-Model-Service-Parent-Schema = "main.default"' in parent_arg

    def test_transient_parent_suppresses_persisted_provider(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        seen = {}
        monkeypatch.setattr(
            codex, "_model_catalog_path", lambda workspace, scope: tmp_path / "models.json"
        )
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda workspace, token, **kwargs: seen.update(kwargs) or {"models": []},
        )

        codex.launch(
            {
                "workspace": WS,
                "provider_services": {"codex": "main.default.developer"},
                "_codex_launch_parent_schema": "main.managed",
            },
            [],
            options=LaunchOptions(),
        )

        assert seen == {
            "source": codex.CodexCatalogSource.PARENT_SCHEMA,
            "identifier": "main.managed",
        }
        provider_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert 'Databricks-Model-Service-Parent-Schema = "main.managed"' in provider_arg
        assert "Databricks-Model-Provider-Service" not in provider_arg

    def test_parent_discovery_refreshes_when_parent_changes(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", tmp_path / "models.json")
        fetched = []

        def fetch(workspace, token, **kwargs):
            fetched.append(kwargs["identifier"])
            return {"models": [{"slug": kwargs["identifier"]}]}

        monkeypatch.setattr(codex, "_fetch_codex_model_catalog", fetch)

        for parent_schema in ("main.first", "main.second"):
            codex.launch(
                {"workspace": WS, "_codex_launch_parent_schema": parent_schema},
                [],
                options=LaunchOptions(),
            )

        catalog_args = [
            next(arg for arg in launch if arg.startswith("model_catalog_json="))
            for launch in launches
        ]
        assert fetched == ["main.first", "main.second"]
        assert catalog_args[0] != catalog_args[1]
        assert json.loads(codex.CODEX_MODEL_CATALOG_PATH.read_text()) == {
            "models": [{"slug": "main.second"}]
        }
        assert read_toml_safe(tmp_path / "config.toml")["model_catalog_json"] == str(
            codex.CODEX_MODEL_CATALOG_PATH
        )

    @pytest.mark.parametrize("tool_args", [[], ["--model", "gpt-mps"]])
    def test_provider_launches_when_discovery_is_unavailable(
        self, tmp_path, monkeypatch, tool_args
    ):
        launches = self._patch(tmp_path, monkeypatch)
        codex.sync_app_model_catalog({"models": [{"slug": "previous-workspace"}]})
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                codex.CodexMpsModelCatalogUnavailable(
                    "codex/v1/models is not enabled for this workspace"
                )
            ),
        )

        codex.launch(
            {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
            tool_args,
            options=LaunchOptions(),
        )

        assert launches
        if tool_args:
            assert launches[0][-len(tool_args) :] == tool_args
        assert not any(arg.startswith("model_catalog_json=") for arg in launches[0])
        assert "model_catalog_json" not in read_toml_safe(tmp_path / "config.toml")
        provider_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert 'Databricks-Model-Provider-Service = "main.default.openai"' in provider_arg

    def test_provider_keeps_other_discovery_failures_fatal(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        monkeypatch.setattr(
            codex,
            "_fetch_codex_model_catalog",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("HTTP 403 Forbidden")),
        )

        with pytest.raises(RuntimeError, match="HTTP 403 Forbidden"):
            codex.launch(
                {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
                [],
                options=LaunchOptions(),
            )

        assert launches == []

    def test_discovery_error_survives_unreadable_shared_config(self, tmp_path, monkeypatch, capsys):
        launches = self._patch(tmp_path, monkeypatch)
        shared_path = tmp_path / "config.toml"
        original = 'model = "unfinished\n'
        shared_path.write_text(original)

        def fail_discovery(*args, **kwargs):
            raise RuntimeError("HTTP 403 Forbidden")

        monkeypatch.setattr(codex, "_fetch_codex_model_catalog", fail_discovery)
        with pytest.raises(RuntimeError, match="HTTP 403 Forbidden"):
            codex.launch(
                {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
                [],
                options=LaunchOptions(),
            )

        assert not launches
        assert shared_path.read_text() == original
        assert "Cannot update Codex App settings" in " ".join(capsys.readouterr().err.split())

    def test_provider_rejects_managed_model_catalog(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        managed_path = tmp_path / "managed_config.toml"
        managed_path.write_text('model_catalog_json = "/admin/models.json"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: managed_path)

        with pytest.raises(RuntimeError, match="overrides model discovery"):
            codex.launch(
                {"workspace": WS, "_codex_launch_provider": "main.default.openai"},
                [],
                options=LaunchOptions(),
            )

        assert launches == []

    def test_non_provider_launch_removes_stale_provider_header(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)
        profile_path = tmp_path / "ucode.config.toml"
        profile_path.write_text(
            profile_path.read_text(encoding="utf-8")
            + "\n[model_providers.Databricks.http_headers]\n"
            + 'Databricks-Model-Provider-Service = "main.default.old"\n',
            encoding="utf-8",
        )

        codex.launch({"workspace": WS}, [], options=LaunchOptions())

        provider_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert "Databricks-Model-Provider-Service" not in provider_arg

    def test_provider_discovery_uses_custom_oauth_token(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        monkeypatch.setenv("OAUTH_TOKEN", "stale-parent-token")
        monkeypatch.setenv("UG_TEST_CUSTOM", "parent")
        execute = Mock()
        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        catalog_path = tmp_path / "models.json"
        seen = {}
        monkeypatch.setattr(codex, "_model_catalog_path", lambda workspace, provider: catalog_path)
        monkeypatch.setattr(
            codex,
            "get_databricks_token",
            lambda *args, **kwargs: pytest.fail("standard token used"),
        )

        def custom_token(
            workspace, client_id, redirect_url, *, scopes, profile, force_refresh=False
        ):
            assert os.environ["UG_TEST_CUSTOM"] == "parent"
            seen["profile"] = profile
            return "custom-token"

        monkeypatch.setattr(codex, "get_custom_client_token", custom_token)

        def fetch(workspace, token, **kwargs):
            assert os.environ["UG_TEST_CUSTOM"] == "parent"
            seen.update(workspace=workspace, token=token, provider=kwargs["identifier"])
            return {"models": [{"slug": "gpt-mps"}]}

        monkeypatch.setattr(codex, "_fetch_codex_model_catalog", fetch)
        state = {
            "workspace": WS,
            "_codex_launch_provider": "main.default.openai",
            "codex_custom_env": {"UG_TEST_CUSTOM": "child"},
            "custom_oauth": {
                "client_id": "client",
                "redirect_url": "http://localhost:8020",
                "scopes": ["all-apis", "offline_access"],
                "profile": "ug-oauth-client",
            },
        }

        codex.launch(state, [], options=LaunchOptions())

        assert seen == {
            "workspace": WS,
            "token": "custom-token",
            "provider": "main.default.openai",
            "profile": "ug-oauth-client",
        }
        assert execute.call_args.kwargs["env"]["OAUTH_TOKEN"] == "custom-token"
        assert execute.call_args.kwargs["env"]["UG_TEST_CUSTOM"] == "child"
        assert os.environ["OAUTH_TOKEN"] == "stale-parent-token"
        assert os.environ["UG_TEST_CUSTOM"] == "parent"

    def test_catalog_paths_are_provider_scoped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", tmp_path / "models.json")

        first = codex._model_catalog_path(WS, "provider:main.default.first")
        second = codex._model_catalog_path(WS, "provider:main.default.second")

        assert first != second
        assert first == codex._model_catalog_path(WS, "provider:main.default.first")

    def test_catalog_write_is_complete_and_atomic(self, tmp_path):
        path = tmp_path / "models.json"
        catalog = {"models": [{"slug": "gpt-mps"}]}

        codex._write_model_catalog(path, catalog)

        assert json.loads(path.read_text(encoding="utf-8")) == catalog
        assert list(tmp_path.glob(".models.json.*.tmp")) == []

    def test_catalog_write_reports_path_on_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "models.json"
        monkeypatch.setattr(codex.os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))

        with pytest.raises(RuntimeError, match=str(path)):
            codex._write_model_catalog(path, {"models": [{"slug": "gpt-mps"}]})

        assert list(tmp_path.glob(".models.json.*.tmp")) == []

    def test_catalog_cleanup_does_not_mask_write_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "models.json"
        monkeypatch.setattr(codex.os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(
            codex.Path,
            "unlink",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
        )

        with pytest.raises(RuntimeError, match=str(path)):
            codex._write_model_catalog(path, {"models": [{"slug": "gpt-mps"}]})

    def test_injects_otel_config_when_tracing_enabled(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        monkeypatch.setenv("UG_TEST_CUSTOM", "parent")
        monkeypatch.setenv("OAUTH_TOKEN", "stale-parent-token")
        parent_bin = tmp_path / "parent-bin"
        child_bin = tmp_path / "child-bin"
        for directory in (parent_bin, child_bin):
            directory.mkdir()
            executable = directory / "codex"
            executable.touch()
            executable.chmod(0o755)
        monkeypatch.setenv("PATH", str(parent_bin))
        server = Mock(server_address=("127.0.0.1", 54321))
        cache = Mock()
        client = Mock()
        process = Mock()
        process.wait.return_value = 0
        popen = Mock(return_value=process)

        def start_otel_proxy(workspace, token_provider):
            assert workspace == WS
            assert os.environ["UG_TEST_CUSTOM"] == "parent"
            assert os.environ["PATH"] == str(parent_bin)
            assert token_provider(False) == "tok"
            return server, cache, client

        monkeypatch.setattr(codex.gateway_proxy, "start_otel_proxy", start_otel_proxy)
        monkeypatch.setattr(codex.subprocess, "Popen", popen)

        with pytest.raises(SystemExit) as exc:
            codex.launch(
                {
                    "workspace": WS,
                    "codex_otel_tracing": True,
                    "codex_custom_env": {
                        "UG_TEST_CUSTOM": "child\nvalue",
                        "UG_TEST_EMPTY": "",
                        "PATH": str(child_bin),
                    },
                },
                ["exec", "hi"],
                options=LaunchOptions(),
            )

        assert exc.value.code == 0
        cache.stop.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        client.close.assert_called_once_with()
        argv = popen.call_args.args[0]
        otel = next((arg for arg in argv if arg.startswith("otel.trace_exporter=")), None)
        assert otel is not None
        assert "http://127.0.0.1:54321/v1/traces" in otel
        assert 'protocol = "binary"' in otel
        assert "Authorization" not in otel  # no credential in argv; the proxy injects it
        assert argv[-2:] == ["exec", "hi"]
        assert argv[0] == str(parent_bin / "codex")
        child = popen.call_args.kwargs["env"]
        assert child["UG_TEST_CUSTOM"] == "child\nvalue"
        assert child["UG_TEST_EMPTY"] == ""
        assert child["PATH"] == str(child_bin)
        assert child["OAUTH_TOKEN"] == "tok"
        assert os.environ["UG_TEST_CUSTOM"] == "parent"
        assert os.environ["OAUTH_TOKEN"] == "stale-parent-token"

    def test_otel_spawn_failure_cleans_proxy_without_mutating_parent(self, tmp_path, monkeypatch):
        self._patch(tmp_path, monkeypatch)
        server = Mock(server_address=("127.0.0.1", 54321))
        cache = Mock()
        client = Mock()
        monkeypatch.setenv("UG_TEST_CUSTOM", "parent")
        monkeypatch.setattr(codex, "resolve_agent_argv", lambda argv: ["/test/codex", *argv[1:]])
        monkeypatch.setattr(
            codex.gateway_proxy, "start_otel_proxy", lambda *args: (server, cache, client)
        )
        popen = Mock(side_effect=OSError("spawn failed"))
        monkeypatch.setattr(codex.subprocess, "Popen", popen)

        with pytest.raises(OSError, match="spawn failed"):
            codex.launch(
                {
                    "workspace": WS,
                    "codex_otel_tracing": True,
                    "codex_custom_env": {"UG_TEST_CUSTOM": "child"},
                },
                [],
                options=LaunchOptions(),
            )

        assert popen.call_args.kwargs["env"]["UG_TEST_CUSTOM"] == "child"
        cache.stop.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        client.close.assert_called_once_with()
        assert os.environ["UG_TEST_CUSTOM"] == "parent"

    def test_no_otel_config_when_tracing_disabled(self, tmp_path, monkeypatch):
        launches = self._patch(tmp_path, monkeypatch)

        codex.launch({"workspace": WS}, ["exec", "hi"], options=LaunchOptions())

        assert not any(arg.startswith("otel=") for arg in launches[0])

    @pytest.mark.parametrize(
        "tool_args",
        [
            ["exec", "hi"],
            ["update"],
            ["app-server", "--listen", "stdio://"],
            ["app", "--new-window"],
        ],
    )
    @pytest.mark.parametrize("version", ["0.134.0", "unknown"])
    def test_layers_profile_as_config_overrides(
        self, tmp_path, monkeypatch, capsys, tool_args, version
    ):
        launches = self._patch(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "agent_version", lambda binary: version)

        codex.launch({"workspace": WS}, tool_args, options=LaunchOptions())

        assert launches[0][0] == "codex"
        assert "--profile" not in launches[0]
        assert launches[0][-len(tool_args) :] == tool_args
        assert 'model_provider="Databricks"' in launches[0]
        provider_arg = next(
            arg for arg in launches[0] if arg.startswith("model_providers.Databricks=")
        )
        assert 'base_url = "https://example.databricks.com/ai-gateway/codex/v1"' in provider_arg
        assert "Upgrade Codex" not in capsys.readouterr().err

    @pytest.mark.parametrize("tool_args", [[], ["exec", "hi"]])
    @pytest.mark.parametrize("stale_profile", [False, True])
    @pytest.mark.parametrize("version", ["0.129.0", "0.133.0"])
    def test_launches_legacy_config_after_configure(
        self, tmp_path, monkeypatch, capsys, tool_args, stale_profile, version
    ):
        profile_path = tmp_path / "ucode.config.toml"
        legacy_path = tmp_path / "config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", profile_path)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", legacy_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "profile-backup.toml")
        monkeypatch.setattr(codex, "LEGACY_CODEX_BACKUP_PATH", tmp_path / "legacy-backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: version)
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        monkeypatch.setattr(codex, "get_databricks_token", lambda *_args, **_kw: "tok")
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        launches: list[list[str]] = []
        monkeypatch.setattr(codex, "exec_or_spawn", lambda argv, *, env: launches.append(argv))
        if stale_profile:
            profile_path.write_text('model_provider = "stale-provider"\n', encoding="utf-8")

        state = codex.write_tool_config({"workspace": WS, "profile": "test-workspace"})

        assert profile_path.exists() is stale_profile
        config = read_toml_safe(legacy_path)
        assert config["profiles"]["ucode"]["model_provider"] == "Databricks"
        assert config["model_providers"]["Databricks"]["base_url"] == (f"{WS}/ai-gateway/codex/v1")

        codex.launch(state, tool_args, options=LaunchOptions())

        assert launches == [["codex", "--profile", "ucode", *tool_args]]
        assert launches[0][:3] == codex.validate_cmd("codex")[:3]
        warning = " ".join(capsys.readouterr().err.split())
        assert f"Codex {version} is outdated" in warning
        assert "Upgrade Codex to 0.134.0 or newer" in warning
        assert "codex --version" in warning

    def test_requires_populated_ucode_profile(self, tmp_path, monkeypatch):
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", tmp_path / "missing.config.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        launches = []
        monkeypatch.setattr(codex, "exec_or_spawn", lambda argv, *, env: launches.append(argv))
        monkeypatch.setattr(codex, "get_databricks_token", lambda *_args, **_kw: "tok")
        monkeypatch.setattr(codex, "clear_model_preferences", lambda state: False)

        with pytest.raises(RuntimeError, match="ucode configure --agents codex"):
            codex.launch({"workspace": WS}, ["update"], options=LaunchOptions())

        assert launches == []


class TestCodexManagedConfig:
    """Every normal configuration also reconciles Codex's OS-managed config."""

    def _patch(self, tmp_path, monkeypatch):
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        managed_path = tmp_path / "etc-codex" / "managed_config.toml"
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "codex-ucode-config.backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        # Deterministic managed path + a mocked sudo writer that writes straight to disk, so the test
        # can read the TOML back and NO real sudo/`/etc` write ever happens.
        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: managed_path)

        def fake_write_managed(path, text, **kwargs):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
            return "written"

        monkeypatch.setattr(codex, "reconcile_managed_file", fake_write_managed)
        return config_path, managed_path

    def test_writes_managed_config_by_default(self, tmp_path, monkeypatch):
        config_path, managed_path = self._patch(tmp_path, monkeypatch)
        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state)

        doc = read_toml_safe(managed_path)
        assert doc["model_provider"] == "Databricks"
        assert "model" not in doc
        assert "Databricks" in doc["model_providers"]
        assert read_toml_safe(config_path)["model_provider"] == "Databricks"

    @pytest.mark.parametrize("previous_provider", ["ucode-databricks", "databricks"])
    def test_reconfigure_selects_databricks_in_existing_profile_and_managed_config(
        self, tmp_path, monkeypatch, previous_provider
    ):
        config_path, managed_path = self._patch(tmp_path, monkeypatch)
        for path in (config_path, managed_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f'model_provider = "{previous_provider}"\n'
                f'[model_providers.{previous_provider}]\nname = "Previous ug provider"\n'
            )

        codex.write_tool_config({"workspace": WS})

        for path in (config_path, managed_path):
            doc = read_toml_safe(path)
            assert doc["model_provider"] == "Databricks"
            assert doc["model_providers"]["Databricks"]["base_url"].startswith(WS)
            assert "auth" in doc["model_providers"]["Databricks"]

    def test_managed_config_preserves_other_keys(self, tmp_path, monkeypatch):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text(
            'model = "my-own"\napproval_policy = "on-request"\n', encoding="utf-8"
        )
        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state)

        doc = read_toml_safe(managed_path)
        # ucode removes its stale model pin, but other keys already in the managed file survive.
        assert doc["approval_policy"] == "on-request"
        assert "model" not in doc

    def _sudo_counting_env(self, tmp_path, monkeypatch):
        """Real reconcile flow (semantic no-op check included) with sudo writes counted."""
        config_path = tmp_path / ".codex" / "ucode.config.toml"
        managed_path = tmp_path / "etc-codex" / "managed_config.toml"
        sudo_writes: list[str] = []
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", config_path)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "codex-ucode-config.backup.toml")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: managed_path)
        monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", tmp_path / "managed-backups")
        monkeypatch.setattr(
            managed_files, "MANAGED_BACKUP_MANIFEST_PATH", tmp_path / "managed-backups" / "m.json"
        )

        def _write(target, text):
            sudo_writes.append(text)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(text, encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", _write)
        return managed_path, sudo_writes

    def test_reapply_unchanged_config_invokes_no_sudo(self, tmp_path, monkeypatch):
        # Codex parity: repeated launches with an unchanged config reach a fixed point with no
        # further privileged writes.
        managed_path, sudo_writes = self._sudo_counting_env(tmp_path, monkeypatch)
        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state)
        baseline = len(sudo_writes)
        assert baseline >= 1
        first_bytes = managed_path.read_bytes()
        codex.write_tool_config(state)
        codex.write_tool_config(state)
        assert len(sudo_writes) == baseline  # semantic no-op: no additional privileged writes
        assert managed_path.read_bytes() == first_bytes

    def test_admin_unrelated_edit_invokes_no_sudo(self, tmp_path, monkeypatch):
        # An admin's unrelated top-level key is preserved and does not force a ug rewrite.
        managed_path, sudo_writes = self._sudo_counting_env(tmp_path, monkeypatch)
        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state)
        baseline = len(sudo_writes)
        # Prepend an unrelated top-level key (valid TOML: bare keys precede table headers).
        managed_path.write_text(
            'approval_policy = "on-request"\n' + managed_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        before = managed_path.read_bytes()
        codex.write_tool_config(state)
        assert len(sudo_writes) == baseline  # unrelated edit did not force a rewrite
        assert managed_path.read_bytes() == before
        assert read_toml_safe(managed_path)["approval_policy"] == "on-request"

    def test_provider_settings_stay_launch_scoped(self, tmp_path, monkeypatch):
        config_path, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text(
            'model_catalog_json = "/tmp/stale.json"\n\n'
            "[model_providers.Databricks.http_headers]\n"
            'Databricks-Model-Provider-Service = "main.default.stale"\n',
            encoding="utf-8",
        )

        codex.write_tool_config(
            {"workspace": WS, "codex_models": ["gpt-5"]},
            provider="main.default.openai",
        )

        local_headers = read_toml_safe(config_path)["model_providers"]["Databricks"]["http_headers"]
        managed = read_toml_safe(managed_path)
        managed_headers = managed["model_providers"]["Databricks"]["http_headers"]
        assert codex.MODEL_PROVIDER_SERVICE_HEADER not in local_headers
        assert codex.MODEL_PROVIDER_SERVICE_HEADER not in managed_headers
        assert managed["model_catalog_json"] == "/tmp/stale.json"

    def test_noninteractive_uses_local_config_when_managed_config_is_compatible(
        self, tmp_path, monkeypatch
    ):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: False)
        state = {"workspace": WS, "codex_models": ["gpt-5"]}
        codex.write_tool_config(state)
        assert not managed_path.exists()

    def test_noninteractive_preserves_unrelated_managed_config(self, tmp_path, monkeypatch):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        original = 'approval_policy = "on-request"\n'
        managed_path.write_text(original, encoding="utf-8")
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: False)

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        assert managed_path.read_text(encoding="utf-8") == original

    def test_noninteractive_fails_when_managed_config_conflicts(self, tmp_path, monkeypatch):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text('model_provider = "enterprise"\n', encoding="utf-8")
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: False)

        with pytest.raises(RuntimeError, match="cannot be applied non-interactively"):
            codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

    def test_invalid_managed_toml_is_not_modified(self, tmp_path, monkeypatch):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text("[invalid", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Cannot safely update Codex managed settings"):
            codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        assert managed_path.read_text(encoding="utf-8") == "[invalid"

    def test_sudo_failure_uses_local_config_when_managed_config_is_compatible(
        self, tmp_path, monkeypatch
    ):
        config_path, _ = self._patch(tmp_path, monkeypatch)
        warnings: list[str] = []
        verified: list[dict] = []

        def deny_managed_write(*args, **kwargs):
            raise managed_files.ManagedFileWriteUnavailable("sudo denied")

        monkeypatch.setattr(
            codex,
            "reconcile_managed_file",
            deny_managed_write,
        )
        monkeypatch.setattr(codex, "print_warning_err", warnings.append)
        monkeypatch.setattr(
            codex,
            "mark_managed_file_verified",
            lambda *args, **kwargs: verified.append(kwargs),
        )

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        assert config_path.exists()
        assert "continuing with local settings" in warnings[0]
        assert verified == [{"scope": "local-compatible"}]

    def test_sudo_failure_remains_fatal_when_managed_config_conflicts(self, tmp_path, monkeypatch):
        _, managed_path = self._patch(tmp_path, monkeypatch)
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_text('model_provider = "enterprise"\n', encoding="utf-8")

        def deny_managed_write(*args, **kwargs):
            raise managed_files.ManagedFileWriteUnavailable("sudo denied")

        monkeypatch.setattr(
            codex,
            "reconcile_managed_file",
            deny_managed_write,
        )

        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="sudo denied"):
            codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})


class TestWriteConfigBackup:
    """A re-configure must not snapshot the file ucode itself generated."""

    def _patch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", tmp_path / "ucode.config.toml")
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "backup.toml")
        monkeypatch.setattr("ucode.config_io.APP_DIR", tmp_path)
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.134.0")
        monkeypatch.setattr(codex, "save_state", lambda state: None)

    def test_first_configure_backs_up_user_owned_profile(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path)
        (tmp_path / "ucode.config.toml").write_text("# user comment\n", encoding="utf-8")

        codex.write_tool_config({"workspace": WS, "codex_models": ["gpt-5"]})

        assert (tmp_path / "backup.toml").read_text(encoding="utf-8") == "# user comment\n"

    def test_reconfigure_does_not_back_up_generated_profile(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path)
        state = {
            "workspace": WS,
            "codex_models": ["gpt-5"],
            # load_state after a first configure: ucode already manages this file.
            "managed_configs": {"codex": {"keys": [["model_provider"]]}},
        }

        codex.write_tool_config(state)

        assert not (tmp_path / "backup.toml").exists()

    def test_clear_model_preferences_does_not_back_up_generated_profile(
        self, tmp_path, monkeypatch
    ):
        self._patch(monkeypatch, tmp_path)
        (tmp_path / "ucode.config.toml").write_text('model = "system.ai.gpt-5"\n', encoding="utf-8")

        changed = codex.clear_model_preferences(
            {"workspace": WS, "managed_configs": {"codex": {"keys": []}}}
        )

        assert changed is True
        assert "model" not in read_toml_safe(tmp_path / "ucode.config.toml")
        assert not (tmp_path / "backup.toml").exists()


class TestCodexManagedMcpUsesManagedFile:
    def test_true_when_supported_and_interactive(self, monkeypatch):
        monkeypatch.setattr(codex, "managed_files_supported", lambda: True)
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        assert codex.managed_mcp_uses_managed_file() is True

    def test_false_when_non_interactive(self, monkeypatch):
        monkeypatch.setattr(codex, "managed_files_supported", lambda: True)
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: False)
        assert codex.managed_mcp_uses_managed_file() is False


class TestCodexReconcileManagedMcp:
    URL = "https://w/ai-gateway/mcp-services/system.ai.github"
    ARGV = ["/opt/ug", "mcp-proxy", "--url", URL, "--host", WS]

    def _wire(self, monkeypatch, existing_text, captured):
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(codex, "read_managed_file", lambda path: existing_text)
        monkeypatch.setattr(codex, "mark_managed_file_verified", lambda *a, **k: None)

        def fake_reconcile(path, desired_text, *, tool, display, owned_paths, parser=None):
            captured.update(text=desired_text, tool=tool, owned_paths=owned_paths)

        monkeypatch.setattr(codex, "reconcile_managed_file", fake_reconcile)

    def test_writes_mcp_servers_preserving_model_keys(self, monkeypatch):
        import tomllib

        captured: dict = {}
        self._wire(monkeypatch, 'model_provider = "Databricks"\n', captured)
        used = codex.reconcile_managed_mcp(
            {}, {"system-ai-github": codex.managed_mcp_entry(self.ARGV)}
        )
        assert used is True
        doc = tomllib.loads(captured["text"])
        assert doc["model_provider"] == "Databricks"
        assert doc["mcp_servers"]["system-ai-github"]["command"] == "/opt/ug"
        assert doc["mcp_servers"]["system-ai-github"]["args"][:2] == ["mcp-proxy", "--url"]
        assert captured["owned_paths"] == [["mcp_servers"]]
        assert captured["tool"] == "codex"

    def test_empty_map_clears_table_preserving_model_keys(self, monkeypatch):
        import tomllib

        captured: dict = {}
        existing = 'model_provider = "Databricks"\n\n[mcp_servers.old]\ncommand = "x"\nargs = []\n'
        self._wire(monkeypatch, existing, captured)
        used = codex.reconcile_managed_mcp({}, {})
        assert used is True
        doc = tomllib.loads(captured["text"])
        assert "mcp_servers" not in doc
        assert doc["model_provider"] == "Databricks"

    def test_clearing_an_absent_table_never_writes(self, monkeypatch):
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(codex, "read_managed_file", lambda path: None)
        monkeypatch.setattr(codex, "mark_managed_file_verified", lambda *a, **k: None)
        monkeypatch.setattr(
            codex, "reconcile_managed_file", lambda *a, **k: pytest.fail("must not write")
        )
        assert codex.reconcile_managed_mcp({}, {}) is True

    def test_non_interactive_returns_false_without_writing(self, monkeypatch):
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: False)
        monkeypatch.setattr(
            codex, "reconcile_managed_file", lambda *a, **k: pytest.fail("must not write")
        )
        assert codex.reconcile_managed_mcp({}, {"s": codex.managed_mcp_entry(self.ARGV)}) is False

    def test_preserves_prior_verification_scope(self, monkeypatch):
        # An MCP-only write must refresh the fingerprint without downgrading the model reconcile's
        # scope (e.g. local-compatible).
        captured: dict = {}
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )
        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(
            codex, "read_managed_file", lambda path: 'model_provider = "Databricks"\n'
        )
        monkeypatch.setattr(codex, "reconcile_managed_file", lambda *a, **k: None)

        def fake_mark(state, tool, path, *, scope="managed"):
            captured["scope"] = scope

        monkeypatch.setattr(codex, "mark_managed_file_verified", fake_mark)
        state = {"managed_file_fingerprints": {"codex": {"scope": "local-compatible"}}}
        codex.reconcile_managed_mcp(state, {"gh": codex.managed_mcp_entry(self.ARGV)})
        assert captured["scope"] == "local-compatible"


class TestCodexReadManagedMcpUrls:
    def test_reads_url_from_proxy_args(self, monkeypatch):
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )
        text = (
            '[mcp_servers.gh]\ncommand = "/opt/ug"\n'
            'args = ["mcp-proxy", "--url", "https://w/mcp-services/x", "--host", "https://w"]\n'
        )
        monkeypatch.setattr(codex, "read_managed_file", lambda path: text)
        assert codex.read_managed_mcp_urls() == {"gh": "https://w/mcp-services/x"}

    def test_empty_when_file_unreadable(self, monkeypatch):
        monkeypatch.setattr(
            codex, "codex_managed_config_path", lambda: Path("/etc/codex/managed_config.toml")
        )

        def boom(path):
            raise RuntimeError("permission denied")

        monkeypatch.setattr(codex, "read_managed_file", boom)
        assert codex.read_managed_mcp_urls() == {}


class TestOtelTokenProvider:
    def test_token_provider_uses_default_profile(self, monkeypatch):
        get_token = Mock(return_value="token")
        monkeypatch.setattr(codex, "get_databricks_token", get_token)
        provider = codex._otel_token_provider({"profile": "myprof"}, WS)
        assert provider(True) == "token"
        get_token.assert_called_once_with(WS, "myprof", force_refresh=True)

    def test_token_provider_uses_custom_oauth_when_configured(self, monkeypatch):
        get_custom_token = Mock(return_value="custom-token")
        get_default_token = Mock()
        monkeypatch.setattr(codex, "get_custom_client_token", get_custom_token)
        monkeypatch.setattr(codex, "get_databricks_token", get_default_token)
        state = {
            "custom_oauth": {
                "client_id": "cid",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
            }
        }
        provider = codex._otel_token_provider(state, WS)
        assert provider(True) == "custom-token"
        assert get_custom_token.call_args.kwargs["force_refresh"] is True
        get_default_token.assert_not_called()


class TestWriteUserMcpServers:
    """Batched user-scope `[mcp_servers]` writes for the workspace-managed reconcile path."""

    def test_adds_and_preserves_other_tables(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text('model = "gpt-5"\n\n[mcp_servers.mine]\ncommand = "x"\nargs = []\n')
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", path)

        entry = codex.managed_mcp_entry(["ug", "mcp-proxy", "https://ws/svc"])
        codex.write_user_mcp_servers({"system-ai-github": entry}, set())

        doc = read_toml_safe(path)
        assert doc["model"] == "gpt-5"  # untouched
        assert dict(doc["mcp_servers"]["mine"]) == {
            "command": "x",
            "args": [],
        }  # developer's own kept
        assert dict(doc["mcp_servers"]["system-ai-github"]) == {
            "command": "ug",
            "args": ["mcp-proxy", "https://ws/svc"],
        }

    def test_removes_named_entries_only(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text('[mcp_servers.gone]\ncommand = "a"\n\n[mcp_servers.mine]\ncommand = "b"\n')
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", path)

        codex.write_user_mcp_servers({}, {"gone"})

        table = read_toml_safe(path)["mcp_servers"]
        assert "gone" not in table
        assert "mine" in table

    def test_writes_to_a_missing_file(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", path)

        codex.write_user_mcp_servers({"a": {"command": "ug", "args": ["x"]}}, set())

        assert dict(read_toml_safe(path)["mcp_servers"]["a"]) == {"command": "ug", "args": ["x"]}

    def test_unparseable_file_falls_back_to_cli_and_does_not_clobber(self, tmp_path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text("this is = = not valid toml [[[")
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", path)
        added: list[tuple[str, list]] = []
        removed: list[str] = []
        import ucode.mcp as mcp_mod

        monkeypatch.setattr(
            mcp_mod, "add_codex_mcp_server", lambda n, argv: added.append((n, argv))
        )
        monkeypatch.setattr(mcp_mod, "remove_codex_mcp_server", lambda n: removed.append(n) or True)

        codex.write_user_mcp_servers(
            {"svc": {"command": "ug", "args": ["mcp-proxy", "u"]}}, {"old"}
        )

        assert path.read_text() == "this is = = not valid toml [[["  # never overwritten
        assert added == [("svc", ["ug", "mcp-proxy", "u"])]
        assert removed == ["old"]

    @pytest.fixture(autouse=True)
    def _clear_codex_home_env(self, monkeypatch):
        monkeypatch.delenv("CODEX_HOME", raising=False)

    def test_honors_codex_home_env(self, tmp_path, monkeypatch):
        # Regression: `codex mcp add` writes config.toml under $CODEX_HOME when set, so a direct
        # write must too.
        codex_home = tmp_path / "codexhome"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        default_path = tmp_path / "default-config.toml"
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", default_path)

        codex.write_user_mcp_servers({"svc": {"command": "ug", "args": ["x"]}}, set())

        written = codex_home / "config.toml"
        assert dict(read_toml_safe(written)["mcp_servers"]["svc"]) == {
            "command": "ug",
            "args": ["x"],
        }
        assert not default_path.exists()


class TestOwnedCodexDestinations:
    @pytest.fixture(autouse=True)
    def isolate(self, tmp_path, monkeypatch):
        self.private = tmp_path / "codex" / "ucode.config.toml"
        self.shared = self.private.parent / "config.toml"
        self.managed = tmp_path / "managed_config.toml"
        self.requirements = tmp_path / "requirements.toml"
        self.catalog = tmp_path / "catalog.json"
        self.private.parent.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", self.private)
        monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", self.shared)
        monkeypatch.setattr(codex, "CODEX_BACKUP_PATH", tmp_path / "profile-backup.toml")
        monkeypatch.setattr(codex, "LEGACY_CODEX_BACKUP_PATH", tmp_path / "legacy-backup.toml")
        monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", self.catalog)
        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: self.managed)
        monkeypatch.setattr(codex, "codex_requirements_path", lambda: self.requirements)
        monkeypatch.setattr(codex, "agent_version", lambda _: "0.154.0")
        monkeypatch.setattr(codex, "ug_version", lambda: "test")
        monkeypatch.setattr(codex, "build_auth_token_argv", lambda *a, **kw: ["ug", "auth-token"])
        monkeypatch.setattr(codex, "_launch_token", lambda *a, **kw: "fixture-token")
        monkeypatch.setattr(codex, "validate_codex_catalog", lambda *a: None)
        monkeypatch.setattr(
            codex,
            "prepare_codex_catalog",
            lambda binary, models: {"models": [{"slug": model} for model in models]},
        )
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(
            managed_files,
            "_SUDO_REPLACE_TARGETS",
            {managed_files.current_os(): {self.managed, self.requirements}},
        )
        self.writes = []

        def privileged_write(path, text, **kwargs):
            if "expected_text" in kwargs:
                assert managed_files.read_managed_file(path) == kwargs["expected_text"]
            self.writes.append(path)
            path.write_text(text, encoding="utf-8")

        monkeypatch.setattr(managed_files, "_sudo_replace", privileged_write)
        self.save = Mock()
        monkeypatch.setattr(codex, "save_state", self.save)

    def source(self, *, workspace=WS, kind="file", owner=None):
        from ucode.managed_source import SelectedManagedSource

        manifest = {"enabled_agents": {"codex": {}}}
        if owner:
            manifest["handoff"] = {
                "schema_version": 1,
                "owner": owner,
                "migration_version": 1,
                "agents": {},
            }
        return SelectedManagedSource(
            kind=kind,
            workspace=workspace,
            agent="codex",
            digest="same-input-digest",
            _manifest_json=json.dumps(manifest) if kind == "file" else None,
        )

    def test_custom_env_lifecycle_owns_runtime_only_and_scrubs_stale_parent(self, monkeypatch):
        monkeypatch.setenv("UG_TEST_RETIRED", "inherited-A")
        monkeypatch.setenv("UG_TEST_CURRENT", "inherited-A")
        monkeypatch.setenv("UG_TEST_UNRELATED", "keep")
        execute = Mock()
        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        initial = {
            "UG_TEST_RETIRED": "A",
            "UG_TEST_CURRENT": "  line one\nline two  ",
            "UG_TEST_EMPTY": "",
        }
        scenarios = [
            (WS, initial),
            ("https://other.databricks.com", {"UG_TEST_CURRENT": "B"}),
            ("https://other.databricks.com", {}),
            ("https://other.databricks.com", {}),
        ]
        for workspace, custom in scenarios:
            state = {"workspace": workspace, "codex_custom_env": custom}
            codex.write_tool_config(state, selected_source=self.source(workspace=workspace))
            codex.launch(state, [], options=LaunchOptions())
            child = execute.call_args.kwargs["env"]
            for key in initial:
                assert child.get(key) == custom.get(key)
            assert child["UG_TEST_UNRELATED"] == "keep"
            assert child["OAUTH_TOKEN"] == "fixture-token"
            for path in (self.private, self.managed):
                native = read_toml_safe(path)
                assert "shell_environment_policy" not in native
                assert "env" not in native
                assert "UG_TEST_" not in path.read_text()
        assert execute.call_count == 4
        assert os.environ["UG_TEST_RETIRED"] == "inherited-A"
        assert os.environ["UG_TEST_CURRENT"] == "inherited-A"

    @pytest.mark.parametrize("hooks_enabled", [False, True])
    def test_native_settings_and_requirements_deliver_preserve_and_release(
        self, monkeypatch, hooks_enabled
    ):
        from ucode.managed_ownership import release_owner

        original = {
            "otel": {"environment": "user-env", "metrics_exporter": "statsig"},
            "features": {"unrelated_flag": True},
            "tui": {"theme": "user-theme"},
        }
        for path in (self.private, self.managed):
            path.write_text(tomlkit.dumps(original))
        self.requirements.write_text('allowed_approval_policies = ["on-request"]\n')
        native = {
            "features": {"hooks": hooks_enabled, "fast_mode": False},
            "tui": {"status_line": ["model-name", "context-remaining"]},
            "model_auto_compact_token_limit": 12000,
            "model_auto_compact_token_limit_scope": "body_after_prefix",
            "otel": {
                "log_user_prompt": False,
                "exporter": "none",
                "trace_exporter": {
                    "otlp-http": {
                        "endpoint": "https://telemetry.example/traces",
                        "protocol": "binary",
                    }
                },
            },
        }
        state = {
            "workspace": WS,
            "codex_native_settings": native,
            "codex_native_requirements": {"features": {"fast_mode": False}},
        }
        codex.write_tool_config(state, selected_source=self.source(owner="isaac"))
        for path in (self.private, self.managed):
            doc = read_toml_safe(path)
            assert doc["features"] == {"unrelated_flag": True, **native["features"]}
            assert doc["tui"] == {"theme": "user-theme", **native["tui"]}
            assert doc["otel"] == {**original["otel"], **native["otel"]}
            assert doc["model_auto_compact_token_limit"] == 12000
            assert doc["model_auto_compact_token_limit_scope"] == "body_after_prefix"
        assert read_toml_safe(self.requirements) == {
            "allowed_approval_policies": ["on-request"],
            "features": {"fast_mode": False},
        }
        assert str(self.requirements) in codex.configured_paths(state)
        execute = Mock()
        monkeypatch.setattr(codex, "exec_or_spawn", execute)
        codex.launch(state, [], options=LaunchOptions())
        argv = execute.call_args.args[0]
        overrides = tomlkit.parse(
            "\n".join(argv[index + 1] for index, arg in enumerate(argv) if arg == "--config")
        )
        assert overrides["features"] == {"unrelated_flag": True, **native["features"]}
        assert overrides["model_auto_compact_token_limit"] == 12000
        assert overrides["tui"] == {"theme": "user-theme", **native["tui"]}
        assert overrides["otel"] == {**original["otel"], **native["otel"]}

        release_owner("isaac", "codex")
        for path in (self.private, self.managed):
            assert read_toml_safe(path) == original
        assert read_toml_safe(self.requirements) == {"allowed_approval_policies": ["on-request"]}

    def test_native_requirements_omission_cleans_registered_destination(self):
        codex.write_tool_config(
            {"workspace": WS, "codex_native_requirements": {"features": {"fast_mode": False}}},
            selected_source=self.source(),
        )
        self.requirements.write_text(self.requirements.read_text() + "\n[admin]\nkeep = true\n")
        codex.write_tool_config({"workspace": WS}, selected_source=self.source())
        assert read_toml_safe(self.requirements) == {"admin": {"keep": True}}

    def test_tracing_override_preserves_private_native_log_metrics_and_environment(
        self, monkeypatch
    ):
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: False)
        native = {
            "otel": {
                "environment": "source-env",
                "exporter": "statsig",
                "metrics_exporter": {
                    "otlp-http": {
                        "endpoint": "https://metrics.example",
                        "protocol": "binary",
                        "headers": {"x.source.scope": "metrics"},
                    }
                },
            }
        }
        state = {"workspace": WS, "codex_otel_tracing": True, "codex_native_settings": native}
        codex.write_tool_config(state, selected_source=self.source())
        assert not self.managed.exists()
        server = Mock(server_address=("127.0.0.1", 54321))
        cache, client = Mock(), Mock()
        monkeypatch.setattr(
            codex.gateway_proxy, "start_otel_proxy", lambda *args: (server, cache, client)
        )
        monkeypatch.setattr(codex, "resolve_agent_argv", lambda argv: ["/test/codex", *argv[1:]])
        process = Mock()
        process.wait.return_value = 0
        popen = Mock(return_value=process)
        monkeypatch.setattr(codex.subprocess, "Popen", popen)

        with pytest.raises(SystemExit) as error:
            codex.launch(state, ["exec", "task"], options=LaunchOptions())

        assert error.value.code == 0
        argv = popen.call_args.args[0]
        layer = {}
        # Pinned Codex builds one CLI layer by splitting paths and assigning in argument order.
        for index, arg in enumerate(argv):
            if arg != "--config":
                continue
            key, _, encoded = argv[index + 1].partition("=")
            path = key.split(".")
            parent = layer
            for segment in path[:-1]:
                parent = parent.setdefault(segment, {})
            parent[path[-1]] = tomlkit.parse("value=" + encoded)["value"]
        assert layer["otel"] == {
            **native["otel"],
            "trace_exporter": {
                "otlp-http": {"endpoint": "http://127.0.0.1:54321/v1/traces", "protocol": "binary"}
            },
        }
        cache.stop.assert_called_once_with()
        server.shutdown.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_managed_and_requirements_backups_revert_independently(self, monkeypatch):
        original_managed = 'admin_managed = "keep"\n'
        original_requirements = 'allowed_approval_policies = ["on-request"]\n'
        self.managed.write_text(original_managed)
        self.requirements.write_text(original_requirements)
        codex.write_tool_config(
            {
                "workspace": WS,
                "codex_native_settings": {"features": {"hooks": True}},
                "codex_native_requirements": {"features": {"fast_mode": False}},
            },
            selected_source=self.source(),
        )
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        entries = [
            entry
            for entry in manifest["files"].values()
            if entry["scope"] in {"managed_settings", "requirements"}
        ]
        assert {entry["path"] for entry in entries} == {str(self.managed), str(self.requirements)}
        assert len({entry["backup_file"] for entry in entries}) == 2
        assert codex.revert_managed_requirements() == "restored"
        assert read_toml_safe(self.requirements) == tomlkit.parse(original_requirements)
        assert read_toml_safe(self.managed)["features"]["hooks"] is True
        assert codex.revert_managed_config() == "restored"
        assert read_toml_safe(self.managed) == tomlkit.parse(original_managed)

    @pytest.mark.parametrize("original_exists", [False, True])
    def test_partial_apply_revert_restores_both_managed_destinations_and_new_siblings(
        self, monkeypatch, original_exists
    ):
        from ucode import managed_ownership

        original_managed = {"admin_managed": "baseline"} if original_exists else {}
        original_requirements = (
            {"allowed_approval_policies": ["on-request"]} if original_exists else {}
        )
        if original_exists:
            self.managed.write_text(tomlkit.dumps(original_managed))
            self.requirements.write_text(tomlkit.dumps(original_requirements))
        shared_original = 'profile = "ucode"\nuser_setting = "baseline"\n'
        self.shared.write_text(shared_original)
        replace = managed_ownership._atomic_replace

        def fail_shared_cleanup(path, text, expected):
            if path == self.shared:
                raise RuntimeError("shared cleanup failed")
            replace(path, text, expected)

        monkeypatch.setattr(managed_ownership, "_atomic_replace", fail_shared_cleanup)
        monkeypatch.setattr(managed_files, "_sudo_remove", lambda path: path.unlink())
        with pytest.raises(RuntimeError, match="shared cleanup failed"):
            codex.write_tool_config(
                {
                    "workspace": WS,
                    "codex_native_settings": {"features": {"hooks": True}},
                    "codex_native_requirements": {"features": {"fast_mode": False}},
                },
                selected_source=self.source(owner="isaac"),
            )
        assert self.writes == [self.managed, self.requirements]
        assert read_toml_safe(self.managed)["features"]["hooks"] is True
        assert read_toml_safe(self.requirements)["features"]["fast_mode"] is False
        pending = managed_ownership.applied_source("codex")
        assert pending is not None and pending["status"] == "pending"
        self.save.assert_not_called()
        for path in (self.managed, self.requirements):
            document = read_toml_safe(path)
            document["admin_after_failure"] = "keep"
            path.write_text(tomlkit.dumps(document))
        monkeypatch.setattr(managed_ownership, "_atomic_replace", replace)

        codex.revert_managed_config()
        codex.revert_managed_requirements()
        assert managed_ownership.revert_owned_destinations("codex")

        assert read_toml_safe(self.managed) == {**original_managed, "admin_after_failure": "keep"}
        assert read_toml_safe(self.requirements) == {
            **original_requirements,
            "admin_after_failure": "keep",
        }
        assert self.shared.read_text() == shared_original
        assert not self.private.exists()
        assert managed_ownership.applied_source("codex") is None
        assert codex.revert_managed_config() == "unchanged"
        assert codex.revert_managed_requirements() == "unchanged"
        assert not managed_ownership.revert_owned_destinations("codex")

    @pytest.mark.parametrize("required", ["settings", "requirements"])
    def test_native_required_policy_cannot_fall_back_when_elevation_unavailable(
        self, monkeypatch, required
    ):
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: False)
        state = {"workspace": WS, f"codex_native_{required}": {"features": {"fast_mode": False}}}
        with pytest.raises(RuntimeError):
            codex.write_tool_config(state, selected_source=self.source())
        assert not self.private.exists()
        assert not self.managed.exists()
        assert not self.requirements.exists()
        self.save.assert_not_called()

    def test_native_requirements_write_failure_retains_pending_without_success(self, monkeypatch):
        from ucode.managed_ownership import applied_source

        writer = managed_files._sudo_replace

        def fail_requirements(path, text, **kwargs):
            if path == self.requirements:
                raise RuntimeError("requirements elevation failed")
            writer(path, text, **kwargs)

        monkeypatch.setattr(managed_files, "_sudo_replace", fail_requirements)
        with pytest.raises(RuntimeError, match="requirements elevation failed"):
            codex.write_tool_config(
                {"workspace": WS, "codex_native_requirements": {"features": {"fast_mode": False}}},
                selected_source=self.source(),
            )
        record = applied_source("codex")
        assert record is not None and record["status"] == "pending"
        self.save.assert_not_called()

    @pytest.mark.parametrize(
        "existing",
        [
            {"otel": "invalid ancestor"},
            {"otel": {"trace_exporter": {"otlp-grpc": {"endpoint": "https://admin.example"}}}},
        ],
    )
    def test_native_incompatible_exporter_base_fails_before_private_mutation(self, existing):
        self.managed.write_text(tomlkit.dumps(existing))
        state = {
            "workspace": WS,
            "codex_native_settings": {
                "otel": {
                    "trace_exporter": {
                        "otlp-http": {"endpoint": "https://source.example", "protocol": "binary"}
                    }
                }
            },
        }
        with pytest.raises(RuntimeError, match="managed_settings.otel"):
            codex.write_tool_config(state, selected_source=self.source())
        assert not self.private.exists()
        assert read_toml_safe(self.managed) == existing
        assert self.writes == []
        self.save.assert_not_called()

    def test_native_status_line_order_empty_and_omission(self):
        for status in (
            ["model-name", "context-remaining"],
            ["context-remaining", "model-name"],
            [],
        ):
            codex.write_tool_config(
                {"workspace": WS, "codex_native_settings": {"tui": {"status_line": status}}},
                selected_source=self.source(),
            )
            for path in (self.private, self.managed):
                assert read_toml_safe(path)["tui"]["status_line"] == status
        codex.write_tool_config({"workspace": WS}, selected_source=self.source())
        for path in (self.private, self.managed):
            assert "status_line" not in read_toml_safe(path).get("tui", {})

    @pytest.mark.parametrize("failure", ["legacy", "platform", "tracing", "requirements"])
    def test_native_preflight_precedes_auth_catalog_and_backup(self, monkeypatch, failure):
        native = (
            {"tui": {"status_line": []}}
            if failure == "legacy"
            else {"otel": {"trace_exporter": "none"}}
            if failure == "tracing"
            else {"features": {"fast_mode": False}}
        )
        if failure == "legacy":
            monkeypatch.setattr(codex, "agent_version", lambda _: "0.99.0")
        elif failure == "platform":
            monkeypatch.setattr(codex, "codex_managed_config_path", lambda: None)
        elif failure == "requirements":
            monkeypatch.setattr(codex, "codex_requirements_path", lambda: None)
        forbidden = Mock(side_effect=AssertionError("side effect before validation"))
        monkeypatch.setattr(codex, "_launch_token", forbidden)
        monkeypatch.setattr(codex, "prepare_managed_catalog", forbidden)
        state = {
            "workspace": WS,
            "codex_native_settings": native,
            "codex_otel_tracing": failure == "tracing",
        }
        if failure == "requirements":
            state["codex_native_requirements"] = {"features": {"fast_mode": False}}
        with pytest.raises(RuntimeError):
            codex.write_tool_config(state, selected_source=self.source())
        with pytest.raises(RuntimeError):
            codex.launch(state, [], options=LaunchOptions())
        forbidden.assert_not_called()
        assert not self.private.exists()
        assert not codex.CODEX_BACKUP_PATH.exists()
        self.save.assert_not_called()

    @pytest.mark.parametrize(
        "custom",
        [
            {"OAUTH_TOKEN": "private-value"},
            {"UG_TEST_CUSTOM": "private\x00value"},
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "private-value"},
        ],
    )
    def test_invalid_resolved_custom_env_fails_before_writes_and_auth(self, monkeypatch, custom):
        token = Mock(side_effect=AssertionError("auth before validation"))
        monkeypatch.setattr(codex, "_launch_token", token)
        state = {"workspace": WS, "codex_otel_tracing": True, "codex_custom_env": custom}
        with pytest.raises(RuntimeError) as error:
            codex.write_tool_config(state, selected_source=self.source())
        assert "private-value" not in str(error.value)
        with pytest.raises(RuntimeError):
            codex.launch(state, [], options=LaunchOptions())
        token.assert_not_called()
        assert not self.private.exists()
        assert not self.managed.exists()
        assert not codex.CODEX_BACKUP_PATH.exists()
        self.save.assert_not_called()

    def test_release_owns_exact_provider_leaves_and_catalog_artifacts(self, capsys):
        from ucode.managed_ownership import release_owner

        self.managed.write_text(
            '[model_providers.Databricks]\ncustom = "keep"\n'
            '[model_providers.Databricks.http_headers]\nX-User = "keep"\n'
        )
        state = {"workspace": WS, "codex_static_models": ["first", "second"]}
        codex.write_tool_config(state, selected_source=self.source(kind="api"))
        assert "restart" in capsys.readouterr().err.lower()
        self.writes.clear()
        codex.write_tool_config(state, selected_source=self.source(owner="isaac"))
        assert self.writes == []
        assert "restart" not in capsys.readouterr().err.lower()
        assert json.loads(self.catalog.read_text())["models"] == [
            {"slug": "first"},
            {"slug": "second"},
        ]
        assert read_toml_safe(self.shared)["model_catalog_json"] == str(self.catalog)
        assert release_owner("isaac", "codex")
        assert not self.catalog.exists()
        assert "model_catalog_json" not in read_toml_safe(self.shared)
        provider = read_toml_safe(self.managed)["model_providers"]["Databricks"]
        assert provider == {"custom": "keep", "http_headers": {"X-User": "keep"}}
        assert "model_provider" not in read_toml_safe(self.private)

    def test_file_to_api_none_removes_edited_header_but_preserves_sibling(self):
        codex.write_tool_config(
            {"workspace": WS, "codex_http_headers": {"X-Old": "policy"}},
            selected_source=self.source(),
        )
        for path in (self.private, self.managed):
            doc = read_toml_safe(path)
            headers = doc["model_providers"]["Databricks"]["http_headers"]
            headers["X-Old"] = "drift"
            headers["X-User"] = "keep"
            path.write_text(codex.tomlkit.dumps(doc))
        codex.write_tool_config({"workspace": WS}, selected_source=self.source(kind="api"))
        for path in (self.private, self.managed):
            headers = read_toml_safe(path)["model_providers"]["Databricks"]["http_headers"]
            assert "X-Old" not in headers
            assert headers["X-User"] == "keep"

    @pytest.mark.parametrize("invalid_target", ["managed", "shared", "catalog"])
    def test_all_destinations_preflight_before_private_write(self, invalid_target):
        self.private.write_text('keep = "private"\n')
        target = getattr(self, invalid_target)
        target.write_text("[malformed")
        with pytest.raises(RuntimeError):
            codex.write_tool_config(
                {"workspace": WS, "codex_static_models": ["first"]},
                selected_source=self.source(),
            )
        assert self.private.read_text() == 'keep = "private"\n'
        assert target.read_text() == "[malformed"
        self.save.assert_not_called()

    def test_catalog_failure_retains_pending_and_retry_converges(self, monkeypatch):
        from ucode.managed_ownership import applied_source

        replace = os.replace

        def replace_except_catalog(src, dst):
            if Path(dst) == self.catalog:
                raise OSError("catalog replacement failed")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_except_catalog)
        state = {"workspace": WS, "codex_static_models": ["first"]}
        with pytest.raises(OSError, match="catalog replacement failed"):
            codex.write_tool_config(state, selected_source=self.source())
        pending = applied_source("codex")
        assert pending is not None and pending["status"] == "pending"
        self.save.assert_not_called()
        self.shared.write_text(self.shared.read_text() + 'personality = "friendly"\n')
        monkeypatch.setattr(os, "replace", replace)
        codex.write_tool_config(state, selected_source=self.source())
        applied = applied_source("codex")
        assert applied is not None and applied["status"] == "applied"
        assert read_toml_safe(self.shared)["personality"] == "friendly"
        assert json.loads(self.catalog.read_text())["models"] == [{"slug": "first"}]

    @pytest.mark.parametrize("source_kind", ["api", "file"])
    @pytest.mark.parametrize("previous_static", [False, True])
    @pytest.mark.parametrize(
        ("provider", "parent_schema"),
        [("main.schema.provider", None), (None, "main.schema")],
    )
    def test_discovery_catalog_is_launch_scoped_committed_and_released(
        self, monkeypatch, source_kind, previous_static, provider, parent_schema
    ):
        from ucode.managed_ownership import release_owner

        fetch = Mock(return_value={"models": [{"slug": "scoped"}]})
        monkeypatch.setattr(codex, "_fetch_codex_model_catalog", fetch)
        state: dict = {"workspace": WS}
        selected = self.source(kind=source_kind, owner="isaac")
        self.private.write_text('personality = "user-value"\n')
        if previous_static:
            state["codex_static_models"] = ["static-model"]
            codex.write_tool_config(state, selected_source=selected)
            assert read_toml_safe(self.private)["model_catalog_json"] == str(self.catalog)
            state.pop("codex_static_models")
        codex.write_tool_config(
            state, provider=provider, parent_schema=parent_schema, selected_source=selected
        )
        scoped = Path(state["_codex_prepared_catalog"]["catalog_path"])
        assert scoped.exists() and self.catalog.exists()
        assert (
            json.loads(scoped.read_text())
            == json.loads(self.catalog.read_text())
            == {"models": [{"slug": "scoped"}]}
        )
        assert "model_catalog_json" not in read_toml_safe(self.private)
        assert "model_catalog_json" not in read_toml_safe(self.managed)
        assert ["model_catalog_json"] not in state["managed_configs"]["codex"]["keys"]
        assert read_toml_safe(self.shared)["model_catalog_json"] == str(self.catalog)
        private_before_launch = self.private.read_bytes()
        state["_codex_launch_provider"] = provider
        state["_codex_launch_parent_schema"] = parent_schema
        monkeypatch.setattr(
            codex, "_write_model_catalog", Mock(side_effect=AssertionError("late write"))
        )
        monkeypatch.setattr(
            codex, "sync_app_model_catalog", Mock(side_effect=AssertionError("late write"))
        )
        launched = Mock()
        monkeypatch.setattr(codex, "_run_codex", launched)
        codex.launch(state, [], options=LaunchOptions())
        assert fetch.call_count == 1
        assert launched.call_count == 1
        launch_catalog = next(
            arg for arg in launched.call_args.args[1] if arg.startswith("model_catalog_json=")
        )
        assert codex.tomlkit.parse(launch_catalog)["model_catalog_json"] == str(scoped)
        assert self.private.read_bytes() == private_before_launch
        assert release_owner("isaac" if source_kind == "file" else "workspace-api", "codex")
        assert not scoped.exists() and not self.catalog.exists()
        assert "model_catalog_json" not in read_toml_safe(self.shared)
        assert read_toml_safe(self.private) == {"personality": "user-value"}

    def test_preserves_custom_app_pointer_and_native_model_default(self):
        self.shared.write_text(
            'model_catalog_json = "/user/catalog.json"\npersonality = "friendly"\n'
        )
        self.managed.write_text('model = "user-model"\n')
        codex.write_tool_config(
            {"workspace": WS, "codex_static_models": ["first"]}, selected_source=self.source()
        )
        assert read_toml_safe(self.shared)["model_catalog_json"] == "/user/catalog.json"
        assert read_toml_safe(self.managed)["model"] == "user-model"
        from ucode.managed_ownership import release_owner

        release_owner("local-file", "codex")
        assert read_toml_safe(self.shared)["model_catalog_json"] == "/user/catalog.json"
        assert read_toml_safe(self.managed)["model"] == "user-model"

    def test_legacy_layout_release_preserves_shared_user_fields(self, monkeypatch):
        from ucode.managed_ownership import release_owner

        monkeypatch.setattr(codex, "agent_version", lambda _: "0.133.0")
        self.shared.write_text('[profiles.ucode]\napproval_policy = "on-request"\n')
        codex.write_tool_config({"workspace": WS}, selected_source=self.source())
        assert read_toml_safe(self.shared)["profile"] == "ucode"
        release_owner("local-file", "codex")
        assert read_toml_safe(self.shared) == {
            "profiles": {"ucode": {"approval_policy": "on-request"}}
        }
        assert not self.private.exists()

    def test_initial_file_catalog_replaces_previous_generated_api_catalog(self):
        self.catalog.write_text('{"models":[{"slug":"retired"}]}')
        codex.write_tool_config(
            {"workspace": WS, "codex_static_models": ["first"]}, selected_source=self.source()
        )
        assert json.loads(self.catalog.read_text())["models"] == [{"slug": "first"}]

    def test_legacy_api_header_table_migrates_once_without_os_snapshot(self, monkeypatch):
        from ucode.managed_ownership import release_owner

        monkeypatch.setattr(codex, "codex_managed_config_path", lambda: None)
        state = {"workspace": WS, "codex_http_headers": {"X-Previous-Api": "old"}}
        codex.write_tool_config(state)
        doc = read_toml_safe(self.private)
        doc["model_providers"]["Databricks"]["http_headers"]["X-Previous-Api"] = "drift"
        doc["model_providers"]["Databricks"]["custom"] = "keep"
        self.private.write_text(codex.tomlkit.dumps(doc))
        state.pop("codex_http_headers")
        codex.write_tool_config(state, selected_source=self.source())
        doc = read_toml_safe(self.private)
        assert "X-Previous-Api" not in doc["model_providers"]["Databricks"]["http_headers"]
        doc["model_providers"]["Databricks"]["http_headers"]["X-New-User"] = "keep"
        self.private.write_text(codex.tomlkit.dumps(doc))
        codex.write_tool_config(state, selected_source=self.source())
        release_owner("local-file", "codex")
        assert read_toml_safe(self.private)["model_providers"]["Databricks"] == {
            "custom": "keep",
            "http_headers": {"X-New-User": "keep"},
        }

    def test_legacy_os_snapshot_supplies_exact_private_header_provenance(self, monkeypatch):
        from ucode.managed_ownership import release_owner

        monkeypatch.setattr(codex, "managed_writes_allowed", lambda: True)
        state = {"workspace": WS, "codex_http_headers": {"X-Previous-Api": "old"}}
        codex.write_tool_config(state)
        for path in (self.private, self.managed):
            doc = read_toml_safe(path)
            provider = doc["model_providers"]["Databricks"]
            provider["http_headers"]["X-Previous-Api"] = "drift"
            provider["http_headers"]["X-New-User"] = "keep"
            provider["custom"] = "keep"
            path.write_text(codex.tomlkit.dumps(doc))
        state.pop("codex_http_headers")
        codex.write_tool_config(state, selected_source=self.source())
        release_owner("local-file", "codex")
        for path in (self.private, self.managed):
            assert read_toml_safe(path)["model_providers"]["Databricks"] == {
                "custom": "keep",
                "http_headers": {"X-New-User": "keep"},
            }
