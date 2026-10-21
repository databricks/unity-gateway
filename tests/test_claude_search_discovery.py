"""Claude-only setup must discover the model used by its search MCP server."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from ucode import cli
from ucode import state as state_module
from ucode.agents import claude
from ucode.databricks import GatewayProbe
from ucode.mcp_web_search import PROVIDER_ENV
from ucode.os_compatibility import subprocess_cross_os

WORKSPACE = "https://example.databricks.com"
CLAUDE_MODEL = "system.ai.claude-opus-4-8"
SEARCH_MODEL = "system.ai.gpt-5-mini"


@pytest.fixture
def search_setup(tmp_path, monkeypatch):
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    config_path = config_dir / ".claude.json"
    isaac_entry = {"command": "existing-isaac-search", "args": []}
    config_path.write_text(json.dumps({"mcpServers": {"web-search": isaac_entry}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv(PROVIDER_ENV, "ucode")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_V2", "0")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", config_dir / "ucode-settings.json")
    monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", tmp_path / "backup.json")
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: None)
    monkeypatch.setattr(claude, "managed_file_snapshots", lambda *_: None)

    # Only external authentication, discovery, managed input, and the Claude CLI
    # are replaced. UG's state persistence and search registration stay real.
    monkeypatch.setattr(cli, "ensure_databricks_auth", lambda *_: None)
    monkeypatch.setattr(cli, "get_databricks_token", lambda *_: "test-token")
    monkeypatch.setattr(
        cli,
        "probe_unity_gateway_capabilities",
        lambda *_: GatewayProbe(True, "reachable", True),
    )
    monkeypatch.setattr(
        cli,
        "discover_model_services",
        lambda *_: ({"opus": CLAUDE_MODEL}, [SEARCH_MODEL], [], [], None),
    )
    monkeypatch.setattr(claude, "refresh_managed_config", lambda *_: SimpleNamespace(manifest=None))

    def run_claude(command, **kwargs):
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, "2.1.286 (Claude Code)\n", "")
        config = json.loads(config_path.read_text())
        if command[:3] == ["claude", "mcp", "remove"]:
            config["mcpServers"].pop(command[3], None)
        elif command[:3] == ["claude", "mcp", "add-json"]:
            config["mcpServers"][command[3]] = json.loads(command[4])
        else:
            raise AssertionError(f"Unexpected external command: {command}")
        config_path.write_text(json.dumps(config))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess_cross_os, "run", run_claude)
    return SimpleNamespace(config_path=config_path, isaac_entry=isaac_entry)


@pytest.mark.parametrize("discovery", ["model_services", "legacy"])
def test_claude_only_setup_registers_search_without_prior_codex_setup(
    search_setup, monkeypatch, discovery
):
    if discovery == "legacy":
        monkeypatch.setattr(
            cli,
            "discover_model_services",
            lambda *_: ({"opus": CLAUDE_MODEL}, [], [], [], None),
        )
        monkeypatch.setattr(cli, "discover_codex_models", lambda *_: ([SEARCH_MODEL], None))
    else:

        def unexpected_fallback(*_):
            raise AssertionError("Available model services must not require legacy discovery")

        monkeypatch.setattr(cli, "discover_codex_models", unexpected_fallback)

    state = cli.configure_shared_state(WORKSPACE, profile="test-profile", tools=["claude"])
    claude.write_tool_config(state, CLAUDE_MODEL)

    servers = json.loads(search_setup.config_path.read_text())["mcpServers"]
    assert servers["web-search"] == search_setup.isaac_entry
    entry = servers["web_search"]
    assert entry["env"] == {
        "DATABRICKS_HOST": WORKSPACE,
        "DATABRICKS_CONFIG_PROFILE": "test-profile",
        "UCODE_WEB_SEARCH_MODEL": SEARCH_MODEL,
    }
    saved = state_module.load_state()
    assert saved["codex_models"] == [SEARCH_MODEL]
    assert saved[claude.WEB_SEARCH_MCP_STATE_KEY] == entry
    settings = json.loads(claude.CLAUDE_SETTINGS_PATH.read_text())
    assert "WebSearch" in settings["permissions"]["deny"]


def test_claude_only_setup_without_search_models_preserves_existing_search(
    search_setup, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "discover_model_services",
        lambda *_: ({"opus": CLAUDE_MODEL}, [], [], [], None),
    )
    monkeypatch.setattr(cli, "discover_codex_models", lambda *_: ([], "No available GPT models"))

    state = cli.configure_shared_state(WORKSPACE, profile="test-profile", tools=["claude"])
    claude.write_tool_config(state, CLAUDE_MODEL)

    assert json.loads(search_setup.config_path.read_text())["mcpServers"] == {
        "web-search": search_setup.isaac_entry
    }
    assert state_module.load_state()["claude_models"] == {"opus": CLAUDE_MODEL}


def test_claude_only_setup_preserves_explicit_search_model(search_setup):
    state_module.save_state({"workspace": WORKSPACE, "web_search_model": "custom-search-model"})

    state = cli.configure_shared_state(WORKSPACE, profile="test-profile", tools=["claude"])
    claude.write_tool_config(state, CLAUDE_MODEL)

    servers = json.loads(search_setup.config_path.read_text())["mcpServers"]
    assert servers["web_search"]["env"]["UCODE_WEB_SEARCH_MODEL"] == "custom-search-model"
    assert servers["web-search"] == search_setup.isaac_entry
