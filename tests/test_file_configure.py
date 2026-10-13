"""`ug configure --file` through the real writers, with only external boundaries mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import agents, cli, codex_config, config_io, managed_config, managed_files, state
from ucode.agents import claude, codex
from ucode.config_io import read_json_safe, read_toml_safe
from ucode.databricks import GatewayProbe

WS = "https://example.databricks.com"
FIXTURES = Path(__file__).parent / "fixtures" / "managed_config"
CLAUDE_MODELS = ["system.ai.claude-sonnet-4-6", "system.ai.claude-opus-4-8"]
CODEX_MODELS = ["system.ai.gpt-5-6-sol", "system.ai.gpt-5-4-nano"]
runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "APP_DIR", config_io.APP_DIR)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    for var in (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "ENABLE_SMART_ROUTING",
        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY",
    ):
        monkeypatch.delenv(var, raising=False)
    paths = {
        "claude": tmp_path / "claude" / "ucode-settings.json",
        "claude_managed": tmp_path / "etc-claude" / "managed-settings.json",
        "codex": tmp_path / "codex" / "ucode.config.toml",
        "codex_managed": tmp_path / "etc-codex" / "managed_config.toml",
    }
    for module, tool in [(claude, "claude"), (codex, "codex")]:
        monkeypatch.setattr(module, f"{tool.upper()}_CONFIG_DIR", paths[tool].parent)
        monkeypatch.setattr(module, f"{tool.upper()}_BACKUP_PATH", tmp_path / f"{tool}.backup")
        monkeypatch.setattr(module, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(module, "ug_version", lambda: "test")
        monkeypatch.setattr(module, "agent_version", lambda _binary: "2.1.268")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", paths["claude"])
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", tmp_path / "claude" / "settings.json")
    monkeypatch.setattr(claude, "CLAUDE_MCP_CONFIG_PATH", tmp_path / ".claude.json")
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: paths["claude_managed"])
    monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", paths["codex"])
    monkeypatch.setattr(codex, "LEGACY_CODEX_CONFIG_PATH", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(codex, "codex_managed_config_path", lambda: paths["codex_managed"])
    monkeypatch.setattr(codex_config, "DEFAULT_CODEX_CONFIG_PATH", paths["codex"])
    monkeypatch.setattr(codex_config, "codex_managed_config_path", lambda: paths["codex_managed"])
    monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)

    def replace_managed(path, text):
        assert path in (paths["claude_managed"], paths["codex_managed"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    monkeypatch.setattr(managed_files, "_sudo_replace", replace_managed)
    cache = tmp_path / "managed-config.json"
    monkeypatch.setattr(managed_config, "MANAGED_CONFIG_PATH", cache)
    # The workspace publishes no managed config; --file must never reach this fetch.
    fetch = Mock(return_value=([], None))
    monkeypatch.setattr(managed_config, "fetch_managed_coding_agent_configs", fetch)
    monkeypatch.setattr(managed_config, "get_databricks_token", lambda *a: "token")

    for name in (
        "install_databricks_cli",
        "ensure_bootstrap_dependencies",
        "ensure_databricks_auth",
    ):
        monkeypatch.setattr(cli, name, Mock())
    monkeypatch.setattr(cli, "run_databricks_login", Mock())
    monkeypatch.setattr(cli, "get_databricks_token", lambda *a, **kw: "token")
    monkeypatch.setattr(cli, "ensure_pat_bearer", Mock(return_value="token"))
    monkeypatch.setattr(cli, "find_profile_name_for_host", lambda _workspace: "selected-profile")
    monkeypatch.setattr(
        cli, "probe_unity_gateway_capabilities", Mock(return_value=GatewayProbe(True, "ok", True))
    )
    # configure only sets up agents whose models it discovers.
    discovered = {"sonnet": "system.ai.claude-sonnet-4-6", "opus": "system.ai.claude-opus-4-8"}
    monkeypatch.setattr(
        cli, "discover_model_services", Mock(return_value=(discovered, [], [], [], None))
    )
    monkeypatch.setattr(cli, "discover_codex_models", Mock(return_value=(CODEX_MODELS, None)))
    monkeypatch.setattr(cli, "install_tool_binary", lambda tool, strict=False: True)
    monkeypatch.setattr(
        codex,
        "prepare_codex_catalog",
        lambda _binary, names: {"models": [{"slug": name, "visibility": "list"} for name in names]},
    )
    # The Claude writer shells out to the `claude` binary, absent on CI runners.
    monkeypatch.setattr(claude, "add_claude_mcp_server", Mock(return_value=None))
    monkeypatch.setattr(claude, "remove_claude_mcp_server", Mock(return_value=False))
    # These resolve against the workspace through the databricks CLI.
    mcp = Mock(return_value=[])
    skills = Mock(return_value=([], []))
    monkeypatch.setattr(cli, "reconcile_managed_mcp_servers", mcp)
    monkeypatch.setattr(cli, "reconcile_managed_skills", skills)
    # Launches must never exec a real agent or hit the separate budget-tier endpoint.
    launch = Mock()
    monkeypatch.setattr(cli, "launch_agent", launch)
    monkeypatch.setattr(cli, "_fetch_budget_recommendation", lambda _state, _managed: None)
    return SimpleNamespace(
        root=tmp_path, paths=paths, cache=cache, fetch=fetch, mcp=mcp, skills=skills, launch=launch
    )


def _configure(fixture: str, *extra: str) -> None:
    result = runner.invoke(
        cli.app,
        [
            "configure",
            "--file",
            str(FIXTURES / fixture),
            "--workspace",
            WS,
            *extra,
            "--skip-upgrade",
        ],
    )
    assert result.exit_code == 0, result.output


def test_file_models_reach_both_writers_and_stick_in_the_cache(home):
    fixture = FIXTURES / "claude_and_codex.json"
    _configure("claude_and_codex.json")

    assert read_json_safe(home.paths["claude"])["availableModels"] == CLAUDE_MODELS
    catalog_path = read_toml_safe(home.paths["codex"])["model_catalog_json"]
    catalog = json.loads(Path(catalog_path).read_text())
    assert [m["slug"] for m in catalog["models"] if m["visibility"] == "list"] == CODEX_MODELS
    home.fetch.assert_not_called()
    cached = read_json_safe(home.cache)
    assert cached["outcome"] == "file"
    assert cached["source_file"] == str(fixture.resolve())


def test_file_selectors_reach_mcp_and_skills_reconcile(home):
    _configure("claude_with_mcp_and_skills.json")

    assert home.mcp.call_args.args[0]["mcp_servers"] == {"names": ["system.ai.github"]}
    assert home.skills.call_args.args[0]["skills"] == {"names": ["system.ai.pdf"]}


@pytest.mark.parametrize(
    ("fixture", "traced"), [("claude_with_tracing.json", True), ("claude.json", False)]
)
def test_tracing_follows_the_file(home, fixture, traced):
    _configure(fixture)

    env = json.loads(home.paths["claude"].read_text()).get("env", {})
    assert ("CLAUDE_CODE_ENABLE_TELEMETRY" in env) is traced
    assert ("OTEL_TRACES_EXPORTER" in env) is traced


# These two re-read the managed config mid-configure, so they prove the override covers every read.
def test_file_headers_replace_developer_headers_wholesale(home):
    home.paths["claude"].parent.mkdir(parents=True)
    home.paths["claude"].write_text(
        json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Developer: dev-value"}})
    )

    _configure("claude_with_headers.json")

    headers = read_json_safe(home.paths["claude"])["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Managed: managed-value" in headers
    assert "X-Developer" not in headers


def test_file_suppresses_ai_tools_self_install(home, monkeypatch):
    install_ai_tools = Mock()
    monkeypatch.setattr(agents, "install_ai_tools", install_ai_tools)

    _configure("claude.json", "--enable-databricks-ai-tools")

    install_ai_tools.assert_not_called()


def _launch(tool: str, *extra: str):
    return runner.invoke(cli.app, [tool, "--workspace", WS, *extra])


def _advance_past_ttl(monkeypatch) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(managed_config, "_utcnow", lambda: future)


def test_plain_claude_launch_uses_the_sticky_file_entry_past_the_ttl(home, monkeypatch):
    _configure("claude.json")
    assert read_json_safe(home.cache)["outcome"] == "file"
    _advance_past_ttl(monkeypatch)

    result = _launch("claude")

    assert result.exit_code == 0, result.output
    home.fetch.assert_not_called()
    assert "Using managed config from" in result.output
    assert read_json_safe(home.paths["claude"])["availableModels"] == CLAUDE_MODELS
    home.launch.assert_called_once()


def test_plain_codex_launch_uses_the_sticky_file_entry_past_the_ttl(home, monkeypatch):
    _configure("claude_and_codex.json")
    _advance_past_ttl(monkeypatch)

    result = _launch("codex")

    assert result.exit_code == 0, result.output
    home.fetch.assert_not_called()
    assert "Using managed config from" in result.output
    catalog_path = read_toml_safe(home.paths["codex"])["model_catalog_json"]
    catalog = json.loads(Path(catalog_path).read_text())
    assert [m["slug"] for m in catalog["models"] if m["visibility"] == "list"] == CODEX_MODELS
    home.launch.assert_called_once()


def test_reconfiguring_with_a_changed_file_changes_the_next_launch(home, tmp_path):
    def config(models: list[str]) -> str:
        return json.dumps(
            {
                "spec_version": 1,
                "enabled_agents": [
                    {
                        "agent": "CODING_AGENT_CLAUDE_CODE",
                        "config": {"models": {"model_services": models}},
                    }
                ],
            }
        )

    path = tmp_path / "dynamic.json"
    path.write_text(config(["system.ai.claude-sonnet-4-6"]))
    _configure(str(path))
    result = _launch("claude")
    assert result.exit_code == 0, result.output
    assert read_json_safe(home.paths["claude"])["availableModels"] == [
        "system.ai.claude-sonnet-4-6"
    ]

    path.write_text(config(["system.ai.claude-opus-4-8"]))
    _configure(str(path))
    result = _launch("claude")
    assert result.exit_code == 0, result.output
    assert read_json_safe(home.paths["claude"])["availableModels"] == ["system.ai.claude-opus-4-8"]
    home.fetch.assert_not_called()


def test_plain_configure_replaces_the_sticky_entry_and_a_later_launch_follows_the_workspace(home):
    _configure("claude.json")
    assert read_json_safe(home.cache)["outcome"] == "file"

    result = runner.invoke(
        cli.app,
        ["configure", "--agents", "claude,codex", "--workspace", WS, "--skip-upgrade"],
    )
    assert result.exit_code == 0, result.output
    home.fetch.assert_called_once()
    assert read_json_safe(home.cache)["outcome"] != "file"

    home.fetch.reset_mock()
    result = _launch("claude")
    assert result.exit_code == 0, result.output
    assert "Using managed config from" not in result.output
    assert "availableModels" not in read_json_safe(home.paths["claude"])


def test_plain_launch_with_no_sticky_entry_fetches_as_today(home):
    result = _launch("claude")

    assert result.exit_code == 0, result.output
    home.fetch.assert_called_once()
    assert "Using managed config from" not in result.output
