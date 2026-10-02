from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import ucode.agents as agents
import ucode.cli as cli
import ucode.managed_files as managed_files
import ucode.state as state
from ucode.agents import claude
from ucode.config_io import restore_file
from ucode.databricks import GatewayProbe
from ucode.managed_config import ManagedConfigResult
from ucode.state import save_state as persist_state

WORKSPACE = "https://example.databricks.com"
RELAY_PROVIDER = "main.default.anthropic-relay"
runner = CliRunner()


@dataclass
class RelayBootstrapHarness:
    private_settings: Path
    managed_settings: Path
    launch_agent: MagicMock

    def launch(self, *args: str):
        return runner.invoke(cli.app, ["claude", *args], catch_exceptions=False)


@pytest.fixture
def relay_bootstrap_harness(tmp_path, monkeypatch) -> RelayBootstrapHarness:
    app_dir = tmp_path / ".ucode"
    private_settings = tmp_path / ".claude" / "ucode-settings.json"
    private_backup = app_dir / "claude-ucode-settings.backup.json"
    managed_settings = tmp_path / "managed-settings.json"
    managed_backup_dir = app_dir / "managed-backups"

    monkeypatch.setattr(state, "APP_DIR", app_dir)
    monkeypatch.setattr(state, "STATE_PATH", app_dir / "state.json")
    monkeypatch.setattr(cli, "save_state", persist_state)
    monkeypatch.setattr(claude, "save_state", persist_state)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", private_settings)
    monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", private_backup)
    monkeypatch.setitem(claude.SPEC, "config_path", private_settings)
    monkeypatch.setitem(claude.SPEC, "backup_path", private_backup)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_settings)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", managed_backup_dir)
    monkeypatch.setattr(
        managed_files,
        "MANAGED_BACKUP_MANIFEST_PATH",
        managed_backup_dir / "manifest.json",
    )
    monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)

    def replace_managed(target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(managed_files, "_sudo_replace", replace_managed)
    monkeypatch.setattr(managed_files, "_sudo_remove", lambda target: target.unlink())
    monkeypatch.setattr(
        claude,
        "refresh_managed_config",
        lambda *_args, **_kwargs: ManagedConfigResult(None, False),
    )
    monkeypatch.setattr(claude, "external_provider_selected", lambda: False)
    monkeypatch.setattr(claude, "ug_version", lambda: "test")
    monkeypatch.setattr(claude, "agent_version", lambda _binary: "test")

    def relayed_proxy_base_url(configured: dict) -> str:
        configured["relayed_proxy_port"] = 12345
        return "http://127.0.0.1:12345"

    monkeypatch.setattr(claude, "relayed_proxy_base_url", relayed_proxy_base_url)

    # Replace only auth, discovery, process, and privileged-write boundaries.
    monkeypatch.setattr(cli, "ensure_bootstrap_dependencies", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_prompt_for_configuration", lambda _tool: (WORKSPACE, None))
    monkeypatch.setattr(cli, "ensure_databricks_auth", lambda *_args: None)
    monkeypatch.setattr(cli, "get_databricks_token", lambda *_args: "test-token")
    monkeypatch.setattr(cli, "find_profile_name_for_host", lambda _workspace: None)
    monkeypatch.setattr(
        cli,
        "probe_unity_gateway_capabilities",
        lambda *_args: GatewayProbe(True, "reachable", True),
    )
    monkeypatch.setattr(
        cli,
        "discover_model_services",
        lambda *_args: ({}, [], [], [], "No hosted Claude models"),
    )
    monkeypatch.setattr(
        cli,
        "discover_claude_models",
        lambda *_args: ({}, "No hosted Claude models"),
    )
    monkeypatch.setattr(cli, "discover_codex_models", lambda *_args: ([], None))
    monkeypatch.setattr(
        cli,
        "_fetch_managed_config",
        lambda _state: ManagedConfigResult(None, False),
    )
    monkeypatch.setattr(cli, "_fetch_budget_recommendation", lambda *_args: None)
    monkeypatch.setattr(cli, "refresh_downloaded_skills_on_launch", lambda _state: None)
    monkeypatch.setattr(cli.smart_routing_v2, "smart_routing_enabled", lambda **_kwargs: False)
    monkeypatch.setattr(
        agents,
        "get_databricks_token",
        lambda *_args: "test-token",
    )
    monkeypatch.setattr(
        agents,
        "resolve_provider_service",
        lambda _tool, provider, *_args: (
            {"relayed": provider == RELAY_PROVIDER, "targets": []},
            None,
        ),
    )
    launch_agent = MagicMock()
    monkeypatch.setattr(cli, "launch_agent", launch_agent)

    return RelayBootstrapHarness(private_settings, managed_settings, launch_agent)


@pytest.mark.parametrize("hosted_models", [True, False], ids=["hosted-models", "relay-only"])
def test_fresh_relay_launch_writes_only_relay_compatible_settings(
    relay_bootstrap_harness: RelayBootstrapHarness, monkeypatch, hosted_models: bool
):
    if hosted_models:
        monkeypatch.setattr(
            cli,
            "discover_model_services",
            lambda *_args: (
                {"sonnet": "databricks-claude-sonnet-4"},
                [],
                [],
                [],
                None,
            ),
        )

    result = relay_bootstrap_harness.launch("--provider", RELAY_PROVIDER)

    assert result.exit_code == 0, result.output
    settings = json.loads(relay_bootstrap_harness.private_settings.read_text())
    assert "apiKeyHelper" not in settings
    assert settings["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert RELAY_PROVIDER in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert not relay_bootstrap_harness.managed_settings.exists()
    configured = state.load_state()
    assert configured["claude_relayed"] is True
    assert configured["available_tools"] == ["claude"]
    assert not configured.get("provider_services")
    assert "Provider: Anthropic" in result.output
    relay_bootstrap_harness.launch_agent.assert_called_once()

    relay_bootstrap_harness.launch_agent.reset_mock()
    repeated = relay_bootstrap_harness.launch("--provider", RELAY_PROVIDER)
    assert repeated.exit_code == 0, repeated.output
    assert not relay_bootstrap_harness.managed_settings.exists()
    relay_bootstrap_harness.launch_agent.assert_called_once()


def test_public_revert_allows_fresh_relay_retry(
    relay_bootstrap_harness: RelayBootstrapHarness, monkeypatch
):
    first = relay_bootstrap_harness.launch("--provider", RELAY_PROVIDER)
    assert first.exit_code == 0, first.output

    def restore_claude_config(config_path: Path, backup_path: Path, managed: bool) -> bool:
        if config_path == relay_bootstrap_harness.private_settings:
            return restore_file(config_path, backup_path, managed)
        return False

    monkeypatch.setattr(cli, "restore_file", restore_claude_config)
    monkeypatch.setattr(cli, "revert_mcp_configs", lambda _state: {})
    monkeypatch.setattr(cli.codex_agent, "revert_managed_config", lambda: "unchanged")
    monkeypatch.setattr(cli, "revert_legacy_shared_config", lambda: False)

    reverted = runner.invoke(cli.app, ["revert"], catch_exceptions=False)

    assert reverted.exit_code == 0, reverted.output
    assert state.load_state() == {}
    assert not relay_bootstrap_harness.private_settings.exists()
    assert not relay_bootstrap_harness.managed_settings.exists()

    relay_bootstrap_harness.launch_agent.reset_mock()
    retried = relay_bootstrap_harness.launch("--provider", RELAY_PROVIDER)
    assert retried.exit_code == 0, retried.output
    assert "apiKeyHelper" not in json.loads(relay_bootstrap_harness.private_settings.read_text())
    assert not relay_bootstrap_harness.managed_settings.exists()
    assert state.load_state()["available_tools"] == ["claude"]
    relay_bootstrap_harness.launch_agent.assert_called_once()


def test_fresh_normal_launch_writes_direct_config_and_marks_available(
    relay_bootstrap_harness: RelayBootstrapHarness, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "discover_model_services",
        lambda *_args: (
            {"sonnet": "databricks-claude-sonnet-4"},
            [],
            [],
            [],
            None,
        ),
    )

    result = relay_bootstrap_harness.launch()

    assert result.exit_code == 0, result.output
    private = json.loads(relay_bootstrap_harness.private_settings.read_text())
    managed = json.loads(relay_bootstrap_harness.managed_settings.read_text())
    assert private["apiKeyHelper"]
    assert managed["apiKeyHelper"] == private["apiKeyHelper"]
    configured = state.load_state()
    assert configured["available_tools"] == ["claude"]
    assert "claude_relayed" not in configured
    relay_bootstrap_harness.launch_agent.assert_called_once()


def test_fresh_normal_launch_without_models_fails_before_writing_settings(
    relay_bootstrap_harness: RelayBootstrapHarness,
):
    result = relay_bootstrap_harness.launch()

    assert result.exit_code == 1
    assert "claude discovery: No" in result.output
    assert "hosted Claude models" in result.output
    assert not relay_bootstrap_harness.private_settings.exists()
    assert not relay_bootstrap_harness.managed_settings.exists()
    assert "claude" not in (state.load_state().get("available_tools") or [])
    relay_bootstrap_harness.launch_agent.assert_not_called()


def test_relay_rejects_existing_admin_auth_settings_without_modifying_them(
    relay_bootstrap_harness: RelayBootstrapHarness,
):
    admin_settings = {
        "apiKeyHelper": "admin-auth-helper",
        "env": {"ANTHROPIC_BASE_URL": "https://admin.example.com"},
        "permissions": {"deny": ["Bash"]},
    }
    original = json.dumps(admin_settings, indent=2) + "\n"
    relay_bootstrap_harness.managed_settings.write_text(original, encoding="utf-8")

    result = relay_bootstrap_harness.launch("--provider", RELAY_PROVIDER)

    assert result.exit_code == 1
    assert "enterprise managed settings" in result.output
    assert "apiKeyHelper" in result.output
    assert "env.ANTHROPIC_BASE_URL" in result.output
    assert relay_bootstrap_harness.managed_settings.read_text() == original
    assert "claude" not in (state.load_state().get("available_tools") or [])
    relay_bootstrap_harness.launch_agent.assert_not_called()
