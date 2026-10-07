from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import tomlkit

from ucode import codex_config
from ucode.agents import codex
from ucode.codex_config import codex_config_args, windows_sandbox_config_args

WS = "https://example.databricks.com"


class TestCodexConfigArgs:
    def test_layers_provider_overrides_without_replacing_user_config(self, monkeypatch):
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")

        overlay = codex.render_overlay(
            WS,
            "gpt-5.6-luna",
            "myprof",
        )
        args = codex_config_args(overlay)

        assert args[:4] == [
            "--config",
            'model_provider="Databricks"',
            "--config",
            'model="gpt-5.6-luna"',
        ]
        provider_override = args[-1]
        assert provider_override.startswith("model_providers.Databricks={")
        assert "/ai-gateway/codex/v1" in provider_override
        assert 'command = "' in provider_override
        assert '"myprof"' in provider_override

    def test_renders_nested_tables_from_parsed_profile(self):
        profile = tomlkit.parse(
            """
model_provider = "ucode-databricks"

[model_providers.ucode-databricks]
name = "Databricks AI Gateway"

[model_providers.ucode-databricks.http_headers]
User-Agent = "ucode"

[model_providers.ucode-databricks.auth]
command = "ucode"
args = ["codex-token"]

[tui.model_availability_nux]
"gpt-5.6-sol" = 1
"""
        )

        args = codex_config_args(profile)

        provider_override = next(
            arg for arg in args if arg.startswith("model_providers.ucode-databricks=")
        )
        assert 'http_headers = {User-Agent = "ucode"}' in provider_override
        assert 'auth = {command = "ucode", args = ["codex-token"]}' in provider_override
        assert 'tui={model_availability_nux = {"gpt-5.6-sol" = 1}}' in args


class TestWindowsSandboxConfigArgs:
    """ug enables Codex's restricted-token Windows sandbox unless the user configured one."""

    @staticmethod
    def _patch_platform(monkeypatch, tmp_path, platform_name):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        monkeypatch.setattr(codex_config, "codex_managed_config_path", lambda: None)
        monkeypatch.setattr(
            codex_config, "os", SimpleNamespace(name=platform_name, environ=os.environ)
        )

    def test_added_on_windows_when_unset(self, monkeypatch, tmp_path):
        self._patch_platform(monkeypatch, tmp_path, "nt")

        assert windows_sandbox_config_args() == [
            "--config",
            'windows.sandbox="unelevated"',
        ]

    @pytest.mark.parametrize("sandbox", ["elevated", "unelevated"])
    def test_omitted_when_user_config_sets_sandbox(self, monkeypatch, tmp_path, sandbox):
        self._patch_platform(monkeypatch, tmp_path, "nt")
        (tmp_path / "config.toml").write_text(f'[windows]\nsandbox = "{sandbox}"\n')

        assert windows_sandbox_config_args() == []

    def test_omitted_when_profile_config_sets_sandbox(self, monkeypatch, tmp_path):
        self._patch_platform(monkeypatch, tmp_path, "nt")
        (tmp_path / "ucode.config.toml").write_text('[windows]\nsandbox = "elevated"\n')

        assert windows_sandbox_config_args() == []

    def test_never_added_on_posix(self, monkeypatch, tmp_path):
        self._patch_platform(monkeypatch, tmp_path, "posix")

        assert windows_sandbox_config_args() == []
