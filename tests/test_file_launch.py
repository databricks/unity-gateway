"""File launch component journeys with actual settings and isolated external boundaries."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from tests.test_managed_source import wire
from ucode import cli, codex_config, config_io, managed_config, managed_files, state
from ucode.agents import claude, codex
from ucode.config_io import read_toml_safe
from ucode.databricks import AnthropicModelCatalog, CodexCatalogSource, GatewayProbe

WS = "https://example.databricks.com"
OTHER_WS = "https://other.databricks.com"
runner = CliRunner()


@pytest.fixture
def launch_home(tmp_path, monkeypatch):
    """Keep real selection, bootstrap orchestration, resolve and file writers in the loop."""
    monkeypatch.setattr(state, "APP_DIR", config_io.APP_DIR)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ENABLE_SMART_ROUTING", raising=False)
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
    captured_stdout = []
    redirect = cli.redirect_output_to_stderr

    def redirect_and_keep_capture_alive():
        captured_stdout.append(sys.stdout)
        redirect()

    monkeypatch.setattr(cli, "redirect_output_to_stderr", redirect_and_keep_capture_alive)
    # These are the external installer, auth, gateway, catalog-subprocess and process boundaries.
    bootstrap = Mock()
    auth = Mock()
    launch = Mock()
    api = Mock(return_value=([], None))
    budget = Mock(return_value=None)
    discover = Mock(return_value=({"sonnet": "system.ai.claude-sonnet-4-6"}, [], [], [], None))
    monkeypatch.setattr(cli, "ensure_bootstrap_dependencies", bootstrap)
    monkeypatch.setattr(cli, "ensure_databricks_auth", auth)
    monkeypatch.setattr(cli, "get_databricks_token", lambda *a, **kw: "token")
    monkeypatch.setattr(cli, "find_profile_name_for_host", lambda _workspace: "selected-profile")
    monkeypatch.setattr(cli, "_prompt_for_configuration", lambda _tool: (WS, "selected-profile"))
    monkeypatch.setattr(
        cli, "probe_unity_gateway_capabilities", lambda *a: GatewayProbe(True, "ok", True)
    )
    monkeypatch.setattr(cli, "discover_model_services", discover)
    monkeypatch.setattr(cli, "discover_codex_models", lambda *a: ([], None))
    monkeypatch.setattr(cli, "launch_agent", launch)
    monkeypatch.setattr(cli, "_fetch_budget_recommendation", budget)
    monkeypatch.setattr(managed_config, "get_databricks_token", lambda *a: "token")
    monkeypatch.setattr(managed_config, "fetch_managed_coding_agent_configs", api)
    monkeypatch.setattr(
        codex,
        "prepare_codex_catalog",
        lambda _binary, names: {"models": [{"slug": name, "visibility": "list"} for name in names]},
    )
    return SimpleNamespace(
        paths=paths,
        bootstrap=bootstrap,
        auth=auth,
        launch=launch,
        api=api,
        budget=budget,
        discover=discover,
        root=tmp_path,
    )


def write_policy(path, agent, model, *, headers=None):
    raw = wire(agent, model)
    if headers is not None:
        raw["enabled_agents"][0]["config"]["http_headers"] = headers
    path.write_text(json.dumps(raw))
    return raw


def write_model_empty_policy(path, agent, config=None):
    raw = wire(agent)
    raw["enabled_agents"][0]["config"] = config or {}
    path.write_text(json.dumps(raw))


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "config", [{}, {"http_headers": {"X-Policy": "local"}}, {"tracing": {"enabled": True}}]
)
def test_model_empty_first_use_keeps_discovery_and_native_selection(launch_home, agent, config):
    h = launch_home
    path = h.root / "minimal.json"
    write_model_empty_policy(path, agent, config)
    result = runner.invoke(cli.app, [agent, "-f", str(path)])
    assert result.exit_code == 0, result.output
    h.discover.assert_called_once_with(WS, "token")
    h.api.assert_not_called()
    h.budget.assert_not_called()
    effective = h.launch.call_args.args[1]
    assert not effective.get(f"{agent}_default_model")
    assert not effective.get(f"{agent}_static_models")
    assert effective.get(f"{agent}_otel_tracing", False) == bool(config.get("tracing"))
    assert state.load_full_state()["workspaces"][WS]["available_tools"] == [agent]
    if agent == "claude":
        assert os.environ[claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR] == "1"
        doc = json.loads(h.paths[agent].read_text())
        assert "ANTHROPIC_MODEL" not in doc["env"]
        assert doc["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"].startswith(
            "system.ai.claude-sonnet-4-6"
        )
        assert "availableModels" not in doc
        assert "modelPicker" not in doc
        if config.get("http_headers"):
            assert "X-Policy: local" in doc["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        if config.get("tracing"):
            assert "otelHeadersHelper" in doc
    else:
        doc = read_toml_safe(h.paths[agent])
        assert "model" not in doc
        assert "model_catalog_json" not in doc
        if config.get("http_headers"):
            assert doc["model_providers"]["Databricks"]["http_headers"]["X-Policy"] == "local"


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("source", ["saved_provider", "explicit_provider", "model_location"])
def test_model_empty_file_keeps_normal_source_selection(launch_home, monkeypatch, agent, source):
    from ucode.agents.args import LaunchOptions

    h = launch_home
    provider = "main.saved.provider" if source == "saved_provider" else "main.explicit.provider"
    initial = {"workspace": WS, "available_tools": [agent], "profile": "saved-profile"}
    if source == "saved_provider":
        initial["provider_services"] = {agent: provider}
    state.save_state(initial)
    resolve = Mock(return_value=(None, None, False))
    monkeypatch.setattr(cli, "resolve_provider_models", resolve)
    catalog_model = "main.catalog.claude-sonnet-4-6"
    catalog = Mock(return_value=AnthropicModelCatalog([catalog_model], {}))
    monkeypatch.setattr(cli, "list_anthropic_model_catalog", catalog)
    path = h.root / "minimal.json"
    write_model_empty_policy(path, agent)
    extra = {
        "saved_provider": [],
        "explicit_provider": ["--provider", provider],
        "model_location": ["--model-location", "main.catalog"],
    }[source]
    result = runner.invoke(cli.app, [agent, "-f", str(path), *extra])
    assert result.exit_code == 0, result.output
    effective = h.launch.call_args.args[1]
    assert effective.get("provider_services", {}) == initial.get("provider_services", {})
    assert state.load_full_state()["workspaces"][WS].get("provider_services", {}) == initial.get(
        "provider_services", {}
    )
    if source != "model_location":
        assert resolve.call_args.args[0] == agent
        assert resolve.call_args.args[2] == provider
        assert effective[f"_{agent}_launch_provider"] == provider
    else:
        resolve.assert_not_called()
    if agent == "claude":
        assert os.environ[claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR] == "1"
        if source == "saved_provider":
            catalog.assert_not_called()
            assert "_claude_launch_default_model" not in effective
        else:
            catalog.assert_called_once_with(
                WS,
                "token",
                **(
                    {"provider": provider}
                    if source == "explicit_provider"
                    else {"parent_schema": "main.catalog"}
                ),
            )
            assert effective["_claude_launch_picker_models"] == [catalog_model]
            assert effective["_claude_launch_default_model"] == catalog_model
            doc = json.loads(h.paths[agent].read_text())
            assert doc["modelPicker"]["options"][0]["model"] == catalog_model
    else:
        process = Mock()
        fetch = Mock(return_value={"models": [{"slug": "gpt-selected", "visibility": "list"}]})
        monkeypatch.setattr(codex, "exec_or_spawn", process)
        monkeypatch.setattr(codex, "get_databricks_token", lambda *a, **kw: "token")
        monkeypatch.setattr(codex, "_fetch_codex_model_catalog", fetch)
        monkeypatch.setattr(codex, "validate_codex_catalog", lambda *a: None)
        codex.launch(effective, ["exec", "hello"], options=LaunchOptions())
        is_parent = source == "model_location"
        fetch.assert_called_once_with(
            WS,
            "token",
            source=CodexCatalogSource.PARENT_SCHEMA if is_parent else CodexCatalogSource.PROVIDER,
            identifier="main.catalog" if is_parent else provider,
        )
        assert 'model="gpt-selected"' in process.call_args.args[0]
        assert ("main.catalog" if is_parent else provider) in str(process.call_args)
    h.api.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_model_empty_workspace_switch_restores_target_bucket(launch_home, monkeypatch, agent):
    h = launch_home
    profiles = {WS: "profile-a", OTHER_WS: "profile-b"}
    custom_oauth = {
        "client_id": "client-b",
        "redirect_url": "http://localhost:8020/callback",
        "scopes": ["offline_access", "model-serving"],
    }
    for workspace, name in [(WS, "a"), (OTHER_WS, "b")]:
        state.save_state(
            {
                "workspace": workspace,
                "available_tools": [agent],
                "profile": profiles[workspace],
                "provider_services": {agent: f"main.{name}.provider"},
                "claude_models": {"sonnet": f"main.{name}.claude-sonnet"},
                "codex_models": [f"main.{name}.gpt"],
                **({"use_pat": True} if workspace == WS else {"custom_oauth": custom_oauth}),
            }
        )
    state.set_current_workspace(WS)
    apply_pat = Mock()
    monkeypatch.setattr(cli, "apply_pat_environment", apply_pat)
    monkeypatch.setattr(cli, "resolve_provider_models", lambda *a: (None, None, False))
    monkeypatch.setattr(claude, "add_claude_mcp_server", lambda *a, **kw: None)
    monkeypatch.setattr(claude, "remove_claude_mcp_server", lambda *a, **kw: None)
    path = h.root / "minimal.json"
    write_model_empty_policy(path, agent)
    for workspace, name in [(WS, "a"), (OTHER_WS, "b"), (WS, "a")]:
        other = OTHER_WS if workspace == WS else WS
        previous_other = state.load_full_state()["workspaces"][other]
        result = runner.invoke(
            cli.app, [agent, "-f", str(path), "--workspace", workspace, "--skip-preflight"]
        )
        assert result.exit_code == 0, result.output
        effective = h.launch.call_args.args[1]
        assert effective["workspace"] == workspace
        assert effective["profile"] == profiles[workspace]
        assert effective["provider_services"][agent] == f"main.{name}.provider"
        assert effective[f"_{agent}_launch_provider"] == f"main.{name}.provider"
        assert effective["claude_models"] == {"sonnet": f"main.{name}.claude-sonnet"}
        assert effective["codex_models"] == [f"main.{name}.gpt"]
        assert effective.get("use_pat", False) == (workspace == WS)
        assert effective.get("custom_oauth") == (custom_oauth if workspace == OTHER_WS else None)
        assert apply_pat.call_args.args[0]["workspace"] == workspace
        full = state.load_full_state()
        assert full["current_workspace"] == workspace
        assert full["workspaces"][other] == previous_other
        auth = (
            json.loads(h.paths[agent].read_text())["apiKeyHelper"]
            if agent == "claude"
            else read_toml_safe(h.paths[agent])["model_providers"]["Databricks"]["auth"]["args"]
        )
        assert workspace in auth
        if workspace == WS:
            assert "--use-pat" in auth
            assert profiles[workspace] in auth
        else:
            assert "--client-id" in auth
            assert "client-b" in auth
    h.auth.assert_not_called()
    h.discover.assert_not_called()
    h.api.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_static_file_can_omit_default_models(launch_home, agent):
    h = launch_home
    path = h.root / "static.json"
    model = "system.ai.claude-sonnet-4-6" if agent == "claude" else "system.ai.gpt-5-6-sol"
    write_model_empty_policy(path, agent, {"models": {"model_services": [model]}})
    result = runner.invoke(cli.app, [agent, "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[1][f"{agent}_static_models"] == [model]
    h.discover.assert_not_called()
    if agent == "claude":
        doc = json.loads(h.paths[agent].read_text())
        assert doc["availableModels"] == [model]
        assert doc["modelPicker"]["options"][0]["model"] == model
    else:
        doc = read_toml_safe(h.paths[agent])
        catalog = json.loads(Path(doc["model_catalog_json"]).read_text())
        assert [entry["slug"] for entry in catalog["models"]] == [model]


@pytest.mark.parametrize("entry_point", ["ug", "ucode"])
@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("option", ["-f", "--config-file"])
def test_first_use_applies_file_with_real_settings_and_preserves_api_cache(
    launch_home, entry_point, agent, option, monkeypatch
):
    h = launch_home
    monkeypatch.chdir(h.root)
    policy = h.root / "policy with spaces.json"
    model = "system.ai.claude-sonnet-4-6" if agent == "claude" else "system.ai.gpt-5-6-sol"
    write_policy(policy, agent, model, headers={"X-Policy": "local"})
    managed_config.MANAGED_CONFIG_PATH.write_bytes(b"cached API bytes must survive\n")
    result = runner.invoke(
        cli.app,
        [agent, option, policy.name, "--", "--model", model, "-f", "agent-input"],
        prog_name=entry_point,
    )
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[2] == ["--model", model, "-f", "agent-input"]
    assert managed_config.MANAGED_CONFIG_PATH.read_bytes() == b"cached API bytes must survive\n"
    h.api.assert_not_called()
    h.budget.assert_not_called()
    h.discover.assert_not_called()
    h.auth.assert_called_once_with(WS, "selected-profile")
    assert state.load_full_state()["workspaces"][WS]["available_tools"] == [agent]
    assert "_manifest_json" not in state.STATE_PATH.read_text()
    assert "local" not in state.STATE_PATH.read_text()
    for key in (agent, f"{agent}_managed"):
        assert h.paths[key].exists()
        if agent == "claude":
            doc = json.loads(h.paths[key].read_text())
            assert "X-Policy: local" in doc["env"]["ANTHROPIC_CUSTOM_HEADERS"]
            assert "ug auth" in doc["apiKeyHelper"]
        else:
            doc = read_toml_safe(h.paths[key])
            assert doc["model"] == model
            provider = doc["model_providers"]["Databricks"]
            assert provider["http_headers"]["X-Policy"] == "local"
            assert "auth" in provider


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize(
    "invalid", ["null", "[]", "{", '{"spec_version":true}', "missing", "directory"]
)
def test_invalid_file_precedes_every_write_or_bootstrap(launch_home, agent, invalid):
    h = launch_home
    policy = h.root / "invalid.json"
    if invalid == "directory":
        policy.mkdir()
    elif invalid != "missing":
        policy.write_text(invalid)
    cache = managed_config.MANAGED_CONFIG_PATH
    cache.write_bytes(b"api-cache")
    before = {path: path.read_bytes() for path in h.root.rglob("*") if path.is_file()}
    result = runner.invoke(cli.app, [agent, "-f", str(policy), "--workspace", OTHER_WS])
    assert result.exit_code != 0
    assert "config-file" in result.output
    assert {path: path.read_bytes() for path in h.root.rglob("*") if path.is_file()} == before
    h.bootstrap.assert_not_called()
    h.auth.assert_not_called()
    h.launch.assert_not_called()
    h.api.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_disabled_agent_and_missing_option_value_do_not_bootstrap(launch_home, agent):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "codex" if agent == "claude" else "claude", "system.ai.model")
    result = runner.invoke(cli.app, [agent, "-f", str(path)])
    assert result.exit_code == 1
    assert "requested agent" in result.output
    result = runner.invoke(cli.app, [agent, "--config-file"])
    assert result.exit_code == 2
    h.bootstrap.assert_not_called()
    h.launch.assert_not_called()


def test_exactly_one_read_and_one_context_through_all_consumers(launch_home, monkeypatch):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "claude", "system.ai.claude-sonnet-4-6", headers={"X-Policy": "selected"})
    read = Path.read_text
    reads = []

    def read_text(current, *args, **kwargs):
        if current == path:
            reads.append(current)
        return read(current, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    h.bootstrap.side_effect = lambda *a, **kw: write_policy(
        path, "claude", "system.ai.claude-opus-4-8", headers={"X-Policy": "changed"}
    )
    sources = []
    for module, name in [
        (cli, "configure_shared_state"),
        (cli, "resolve_state"),
        (cli, "configure_tool"),
        (claude, "write_tool_config"),
    ]:
        original = getattr(module, name)

        def record(*a, _original=original, **kw):
            sources.append(kw["selected_source"])
            return _original(*a, **kw)

        monkeypatch.setattr(module, name, record)
    monkeypatch.setattr(
        claude,
        "refresh_managed_config",
        lambda *a, **kw: pytest.fail("writer must use selected source"),
    )
    result = runner.invoke(cli.app, ["claude", "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert reads == [path]
    assert len(sources) == 4
    assert all(source is sources[0] for source in sources)
    assert sources[0].workspace == WS
    assert sources[0].agent == "claude"
    doc = json.loads(h.paths["claude"].read_text())
    assert "X-Policy: selected" in doc["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert "changed" not in h.paths["claude"].read_text()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_source_transition_matrix_and_file_rereads_inside_ttl(launch_home, agent):
    h = launch_home
    state.save_state({"workspace": WS, "available_tools": [agent]})
    prefix = "claude-sonnet" if agent == "claude" else "gpt"
    a, b, api_model = (f"system.ai.{prefix}-{name}" for name in ("a", "b", "api"))
    path_a, path_b = h.root / "a.json", h.root / "b.json"
    write_policy(path_a, agent, a, headers={"X-Policy": "a"})
    write_policy(path_b, agent, b, headers={"X-Policy": "b"})
    api_raw = wire(agent, api_model)
    api_raw["enabled_agents"][0]["config"]["http_headers"] = {"X-Policy": "api"}
    h.api.return_value = ([api_raw], None)

    def launch(args, expected):
        result = runner.invoke(cli.app, [agent, *args])
        assert result.exit_code == 0, result.output
        effective = h.launch.call_args.args[1]
        assert effective.get(f"{agent}_default_model") == expected
        for key in (agent, f"{agent}_managed"):
            text = h.paths[key].read_text()
            if expected is not None:
                assert expected in text
            if agent == "claude":
                headers = json.loads(text)["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
                assert "X-Policy: a" not in headers or expected == a

    launch([], api_model)
    api_bytes = managed_config.MANAGED_CONFIG_PATH.read_bytes()
    launch(["-f", str(path_a)], a)
    launch(["-f", str(path_b)], b)
    write_policy(path_b, agent, a)
    launch(["-f", str(path_b), "--refresh"], a)
    assert managed_config.MANAGED_CONFIG_PATH.read_bytes() == api_bytes
    launch([], api_model)
    assert h.api.call_count == 1
    h.api.return_value = ([], None)
    launch(["-f", str(path_b)], a)
    managed_config.save_managed_state(WS, None, outcome="none")
    launch([], None)
    assert h.api.call_count == 1
    assert json.loads(managed_config.MANAGED_CONFIG_PATH.read_text())["outcome"] == "none"


@pytest.mark.parametrize("cached", [False, True])
def test_file_never_becomes_api_fallback_after_fetch_failure(launch_home, cached):
    h = launch_home
    path = h.root / "local.json"
    write_policy(path, "codex", "system.ai.gpt-file")
    assert runner.invoke(cli.app, ["codex", "-f", str(path)]).exit_code == 0
    if cached:
        managed_config.save_managed_state(
            WS, wire("codex", "system.ai.gpt-api"), outcome="published"
        )
    h.api.return_value = ([], "HTTP 503 temporary")
    result = runner.invoke(cli.app, ["codex", "--refresh"])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[1].get("codex_default_model") == (
        "system.ai.gpt-api" if cached else None
    )
    assert h.api.call_count == (0 if cached else 1)


@pytest.mark.parametrize("kind", ["mcp", "skills"])
@pytest.mark.parametrize("foreign", [False, True])
def test_resource_transition_preflight(launch_home, kind, foreign):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "codex", "system.ai.gpt-file")
    state.save_state(
        {
            "workspace": WS,
            "available_tools": ["codex"],
            "managed_mcp_servers": [
                {"name": "service", "url": f"{WS}/api/2.0/mcp/services/a/b/c", "clients": ["codex"]}
            ]
            if kind == "mcp"
            else [],
        }
    )
    if kind == "skills":
        (config_io.APP_DIR / "skills.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skill_downloads": [{"scope": "managed", "workspace": WS, "fqn": "a.b.c"}],
                }
            )
        )
    before = state.STATE_PATH.read_bytes()
    result = runner.invoke(
        cli.app, ["codex", "-f", str(path), "--workspace", OTHER_WS if foreign else WS]
    )
    if foreign:
        assert result.exit_code == 1, result.output
        assert "require migration" in result.output
        assert state.STATE_PATH.read_bytes() == before
        h.bootstrap.assert_not_called()
        h.launch.assert_not_called()
    else:
        assert result.exit_code == 0, result.output
        assert "outside the config file" in " ".join(result.output.split())


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_same_workspace_app_mcp_remains_configured(launch_home, agent):
    h = launch_home
    server = {
        "name": "workspace-app",
        "url": "https://workspace-app.example.databricksapps.com/mcp",
        "auth": "proxy",
        "clients": [agent],
    }
    state.save_state({"workspace": WS, "available_tools": [agent], "mcp_servers": [server]})
    path = h.root / "minimal.json"
    write_model_empty_policy(path, agent)
    result = runner.invoke(cli.app, [agent, "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[1]["mcp_servers"] == [server]
    assert state.load_full_state()["workspaces"][WS]["mcp_servers"] == [server]
    assert "require migration" not in result.output


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_first_use_preserves_unrelated_os_managed_mcp(launch_home, agent):
    h = launch_home
    managed_path = h.paths[f"{agent}_managed"]
    managed_path.parent.mkdir()
    url = "https://isaac-mcp.example.com/mcp"
    if agent == "claude":
        server = {"url": url, "type": "http"}
        managed_path.write_text(json.dumps({"managedMcpServers": {"isaac": server}}))
    else:
        server = {"command": "isaac-mcp", "args": ["--url", url]}
        managed_path.write_text(
            f'[mcp_servers.isaac]\ncommand = "isaac-mcp"\nargs = ["--url", "{url}"]\n'
        )
    path = h.root / "minimal.json"
    write_model_empty_policy(path, agent)
    result = runner.invoke(cli.app, [agent, "-f", str(path)])
    assert result.exit_code == 0, result.output
    assert state.load_full_state()["workspaces"][WS]["available_tools"] == [agent]
    if agent == "claude":
        assert json.loads(managed_path.read_text())["managedMcpServers"]["isaac"] == server
    else:
        assert read_toml_safe(managed_path)["mcp_servers"]["isaac"] == server
    assert "require migration" not in result.output


@pytest.mark.parametrize("kind", ["mcp", "skills"])
def test_no_file_workspace_switch_keeps_ordinary_setup_path(launch_home, monkeypatch, kind):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "codex", "system.ai.gpt-file")
    assert runner.invoke(cli.app, ["codex", "-f", str(path)]).exit_code == 0
    if kind == "skills":
        (config_io.APP_DIR / "skills.json").write_text(
            json.dumps({"version": 1, "skill_downloads": [{"scope": "managed", "workspace": WS}]})
        )
    else:
        existing = state.load_state()
        existing["managed_mcp_servers"] = [
            {"name": "service", "url": f"{WS}/api/2.0/mcp/services/a/b/c", "clients": ["codex"]}
        ]
        state.save_state(existing)
    setup = Mock(side_effect=RuntimeError("ordinary setup reached"))
    monkeypatch.setattr(cli, "_auto_configure_tool", setup)
    h.launch.reset_mock()
    result = runner.invoke(cli.app, ["codex", "--workspace", OTHER_WS])
    assert result.exit_code == 1, result.output
    assert "ordinary setup reached" in result.output
    setup.assert_called_once_with("codex")
    h.launch.assert_not_called()


def test_invalid_file_does_not_mutate_existing_codex_model_on_state_read(launch_home):
    h = launch_home
    state.save_state({"workspace": WS, "available_tools": ["codex"]})
    h.paths["codex"].parent.mkdir(parents=True, exist_ok=True)
    h.paths["codex"].write_text('model = "user-model"\n')
    path = h.root / "invalid.json"
    path.write_text("null")
    result = runner.invoke(cli.app, ["codex", "-f", str(path)])
    assert result.exit_code == 1
    assert h.paths["codex"].read_text() == 'model = "user-model"\n'
    h.bootstrap.assert_not_called()


@pytest.mark.parametrize("static", [False, True])
def test_file_suppresses_saved_provider_at_final_codex_launch_and_restores_api_preference(
    launch_home, monkeypatch, static
):
    from ucode.agents.args import LaunchOptions

    h = launch_home
    existing = {
        "workspace": WS,
        "available_tools": ["codex"],
        "provider_services": {"codex": "main.saved.provider"},
    }
    state.save_state(existing)
    path = h.root / "config.json"
    raw = wire("codex", "system.ai.gpt-file")
    if static:
        raw["enabled_agents"][0]["config"]["models"] = {"model_services": ["system.ai.gpt-file"]}
    path.write_text(json.dumps(raw))
    result = runner.invoke(cli.app, ["codex", "-f", str(path)])
    assert result.exit_code == 0, result.output
    effective = h.launch.call_args.args[1]
    assert not effective["provider_services"]
    assert (
        state.load_full_state()["workspaces"][WS]["provider_services"]["codex"]
        == "main.saved.provider"
    )
    process = Mock()
    monkeypatch.setattr(codex, "exec_or_spawn", process)
    monkeypatch.setattr(codex, "get_databricks_token", lambda *a, **kw: "token")
    monkeypatch.setattr(
        codex,
        "_fetch_codex_model_catalog",
        lambda *a, **kw: pytest.fail("saved provider was incorrectly selected"),
    )
    codex.launch(effective, ["exec", "hello"], options=LaunchOptions())
    assert "main.saved.provider" not in str(process.call_args)
    monkeypatch.setattr(cli, "resolve_provider_models", lambda *a: (None, None, False))
    result = runner.invoke(cli.app, ["codex"])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[1]["_codex_launch_provider"] == "main.saved.provider"


@pytest.mark.parametrize(
    "args", [["update"], ["exec", "hello"], ["app-server", "--listen", "stdio://"], ["--", "hello"]]
)
def test_codex_command_meaning_and_protocol_stdout(launch_home, args):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "codex", "system.ai.gpt-file")
    result = runner.invoke(cli.app, ["codex", "-f", str(path), *args])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[2] == (args[1:] if args[0] == "--" else args)
    if args[0] == "app-server":
        assert result.stdout == ""
        assert "Starting Codex" in result.stderr


def test_claude_caller_settings_remain_forwarded(launch_home):
    h = launch_home
    path = h.root / "config.json"
    write_policy(path, "claude", "system.ai.claude-sonnet-4-6")
    caller = h.root / "caller settings.json"
    caller.write_text('{"userSetting": true}')
    args = ["--settings", str(caller), "--model", "user-model", "--print", "hello"]
    result = runner.invoke(cli.app, ["claude", "-f", str(path), "--", *args])
    assert result.exit_code == 0, result.output
    assert h.launch.call_args.args[2] == args
    assert h.launch.call_args.kwargs["options"].user_pinned_model == "user-model"
    assert caller.read_text() == '{"userSetting": true}'
