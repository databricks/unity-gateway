"""Tests for agents/gemini.py."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from ucode import mcp
from ucode.agents import gemini
from ucode.agents.interface import Agent, ConfigureRequest, McpClient, McpServer

WS = "https://example.databricks.com"


@pytest.fixture(autouse=True)
def _redirect_gemini_home(tmp_path, monkeypatch):
    gemini_home = tmp_path / ".gemini-home"
    monkeypatch.setattr(gemini, "GEMINI_HOME_DIR", gemini_home)
    monkeypatch.setattr(gemini, "GEMINI_SETTINGS_PATH", gemini_home / ".gemini" / "settings.json")


class TestGeminiSpec:
    def test_binary(self):
        assert gemini.SPEC["binary"] == "gemini"

    def test_package(self):
        assert gemini.SPEC["package"] == "@google/gemini-cli"

    def test_display(self):
        assert gemini.SPEC["display"] == "Gemini CLI"

    def test_config_path_is_ucode_env_file(self):
        assert gemini.SPEC["config_path"].name == "ucode.env"


class TestRenderEnvOverlay:
    def test_sets_gemini_model(self):
        env = gemini.render_env_overlay(WS, "gemini-2.0-flash", "tok123")
        assert env["GEMINI_MODEL"] == "gemini-2.0-flash"

    def test_sets_base_url(self):
        env = gemini.render_env_overlay(WS, "gemini-2.0-flash", "tok123")
        assert env["GOOGLE_GEMINI_BASE_URL"] == f"{WS}/ai-gateway/gemini"

    def test_sets_api_key(self):
        env = gemini.render_env_overlay(WS, "gemini-2.0-flash", "tok123")
        assert env["GEMINI_API_KEY"] == "tok123"

    def test_sets_oauth_token_for_mcp(self):
        env = gemini.render_env_overlay(WS, "gemini-2.0-flash", "tok123")
        assert env["OAUTH_TOKEN"] == "tok123"

    def test_sets_bearer_auth_mechanism(self):
        env = gemini.render_env_overlay(WS, "gemini-2.0-flash", "tok123")
        assert env["GEMINI_API_KEY_AUTH_MECHANISM"] == "bearer"

    def test_sets_user_agent_via_custom_headers(self, monkeypatch):
        monkeypatch.setattr(gemini, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.40.0")
        env = gemini.render_env_overlay(WS, "gemini-2", "tok")
        assert env["GEMINI_CLI_CUSTOM_HEADERS"] == "User-Agent:ucode/0.1.0 gemini/0.40.0"

    def test_provider_adds_routing_header_and_pins_target(self, monkeypatch):
        monkeypatch.setattr(gemini, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.40.0")
        env = gemini.render_env_overlay(
            WS, "gemini-3.5-flash", "tok", provider="cat.sch.gemini-enterprise"
        )
        assert env["GEMINI_MODEL"] == "gemini-3.5-flash"
        assert env["GEMINI_CLI_CUSTOM_HEADERS"] == (
            "User-Agent:ucode/0.1.0 gemini/0.40.0,"
            "Databricks-Model-Provider-Service:cat.sch.gemini-enterprise"
        )


class TestBuildRuntimeEnv:
    def test_merges_os_environment(self):
        env = gemini.build_runtime_env(WS, "gemini-2", "tok")
        assert "PATH" in env

    def test_overrides_gemini_vars(self):
        env = gemini.build_runtime_env(WS, "gemini-2.0-flash", "mytoken")
        assert env["GEMINI_MODEL"] == "gemini-2.0-flash"
        assert env["GEMINI_API_KEY"] == "mytoken"
        assert env["GEMINI_API_KEY_AUTH_MECHANISM"] == "bearer"

    def test_sets_base_url(self):
        env = gemini.build_runtime_env(WS, "gemini-2", "tok")
        assert env["GOOGLE_GEMINI_BASE_URL"] == f"{WS}/ai-gateway/gemini"

    def test_sets_oauth_token_for_mcp(self):
        env = gemini.build_runtime_env(WS, "gemini-2", "tok")
        assert env["OAUTH_TOKEN"] == "tok"

    def test_sets_private_gemini_home(self, tmp_path, monkeypatch):
        gemini_home = tmp_path / "private-gemini-home"
        monkeypatch.setattr(gemini, "GEMINI_HOME_DIR", gemini_home)
        monkeypatch.setattr(
            gemini, "GEMINI_SETTINGS_PATH", gemini_home / ".gemini" / "settings.json"
        )

        env = gemini.build_runtime_env(WS, "gemini-2", "tok")

        assert env["GEMINI_CLI_HOME"] == str(gemini_home)

    def test_writes_private_auth_settings(self):
        gemini.build_runtime_env(WS, "gemini-2", "tok")

        settings = json.loads(gemini.GEMINI_SETTINGS_PATH.read_text())
        assert settings == {"security": {"auth": {"selectedType": "gemini-api-key"}}}

    def test_preserves_existing_private_settings(self):
        gemini.GEMINI_SETTINGS_PATH.parent.mkdir(parents=True)
        gemini.GEMINI_SETTINGS_PATH.write_text(
            json.dumps({"theme": "dark", "security": {"auth": {"selectedType": "oauth"}}})
        )

        gemini.build_runtime_env(WS, "gemini-2", "tok")

        settings = json.loads(gemini.GEMINI_SETTINGS_PATH.read_text())
        assert settings["theme"] == "dark"
        assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"


class TestGeminiDefaultModel:
    def test_returns_first_model(self):
        state = {"gemini_models": ["gemini-2", "gemini-1"]}
        assert gemini.default_model(state) == "gemini-2"

    def test_returns_none_when_empty_list(self):
        assert gemini.default_model({"gemini_models": []}) is None

    def test_returns_none_when_missing(self):
        assert gemini.default_model({}) is None


class TestGeminiVersionGating:
    def test_too_new_version_flags_045(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.45.0-nightly.20260602")
        assert gemini.too_new_version() == "0.45.0-nightly.20260602"

    def test_too_new_version_allows_044(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.44.1")
        assert gemini.too_new_version() is None

    def test_too_new_version_none_when_unknown(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "unknown")
        assert gemini.too_new_version() is None

    def test_too_new_downgrade_returns_target(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.45.0-nightly.20260602")
        monkeypatch.setattr(gemini, "latest_version_below", lambda pkg, ceiling: "0.44.1")
        assert gemini.too_new_downgrade() == ("0.45.0-nightly.20260602", "0.44.1")

    def test_too_new_downgrade_none_when_safe(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.44.1")
        monkeypatch.setattr(gemini, "latest_version_below", lambda pkg, ceiling: "0.44.1")
        assert gemini.too_new_downgrade() is None

    def test_too_new_downgrade_none_when_no_target(self, monkeypatch):
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.45.0")
        monkeypatch.setattr(gemini, "latest_version_below", lambda pkg, ceiling: None)
        assert gemini.too_new_downgrade() is None


class TestGeminiValidateCmd:
    def test_starts_with_binary(self):
        cmd = gemini.validate_cmd("gemini")
        assert cmd[0] == "gemini"

    def test_has_p_flag(self):
        cmd = gemini.validate_cmd("gemini")
        assert "-p" in cmd

    def test_has_prompt_text(self):
        cmd = gemini.validate_cmd("gemini")
        assert len(cmd) > 2


class TestGeminiManagedKeys:
    def test_managed_keys_not_empty(self):
        assert len(gemini.MANAGED_KEYS) > 0

    def test_managed_keys_includes_model(self):
        assert "GEMINI_MODEL" in gemini.MANAGED_KEYS

    def test_managed_keys_includes_api_key(self):
        assert "GEMINI_API_KEY" in gemini.MANAGED_KEYS

    def test_managed_keys_includes_oauth_token(self):
        assert "OAUTH_TOKEN" in gemini.MANAGED_KEYS


class TestWriteToolConfig:
    def test_writes_ucode_env_file(self, tmp_path, monkeypatch):
        import ucode.config_io as config_io_mod

        env_path = tmp_path / "ucode.env"
        monkeypatch.setattr(gemini, "GEMINI_ENV_PATH", env_path)
        monkeypatch.setattr(gemini, "GEMINI_BACKUP_PATH", tmp_path / "backup")
        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr("ucode.agents.gemini.save_state", lambda s: None)
        monkeypatch.setattr(
            "ucode.agents.gemini.get_databricks_token",
            lambda ws, profile=None, **kwargs: "fake-token",
        )

        gemini.write_tool_config({"workspace": WS}, "some-model")

        env = env_path.read_text()
        assert 'GEMINI_MODEL="some-model"' in env
        assert f'GOOGLE_GEMINI_BASE_URL="{WS}/ai-gateway/gemini"' in env

    def test_does_not_write_settings_json(self, tmp_path, monkeypatch):
        import ucode.config_io as config_io_mod

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"theme": "dark", "otherKey": 123}))
        monkeypatch.setattr(gemini, "GEMINI_ENV_PATH", tmp_path / "ucode.env")
        monkeypatch.setattr(gemini, "GEMINI_BACKUP_PATH", tmp_path / "backup")
        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr("ucode.agents.gemini.save_state", lambda s: None)
        monkeypatch.setattr(
            "ucode.agents.gemini.get_databricks_token",
            lambda ws, profile=None, **kwargs: "fake-token",
        )

        gemini.write_tool_config({"workspace": WS}, "some-model")

        settings = json.loads(settings_path.read_text())
        assert settings["theme"] == "dark"
        assert settings["otherKey"] == 123
        assert "security" not in settings


class TestWriteUserMcpServers:
    def test_batched_add_remove_preserves_other_settings(self, tmp_path, monkeypatch):
        path = tmp_path / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"security": {"x": 1}, "mcpServers": {"mine": {"command": "z"}}})
        )
        monkeypatch.setattr(gemini, "GEMINI_SETTINGS_PATH", path)

        entry = gemini.build_mcp_server_entry(["ug", "mcp-proxy", "u"])
        gemini.write_user_mcp_servers({"svc": entry}, {"mine"})

        doc = json.loads(path.read_text())
        assert doc["security"] == {"x": 1}  # untouched
        assert "mine" not in doc["mcpServers"]
        assert doc["mcpServers"]["svc"] == {"command": "ug", "args": ["mcp-proxy", "u"]}


PROXY_ARGV = ("/usr/bin/ug", "mcp-proxy", "--url", "https://example.databricks.com/mcp/x")


def _server(**kwargs) -> McpServer:
    return McpServer(url="https://example.databricks.com/mcp/x", proxy_argv=PROXY_ARGV, **kwargs)


class TestGeminiAgentDescription:
    def test_is_an_agent_with_an_mcp_client(self):
        assert isinstance(gemini.AGENT, Agent)
        assert isinstance(gemini.AGENT.mcp, McpClient)

    def test_describes_itself_from_its_spec(self):
        assert gemini.AGENT.display == "Gemini CLI"
        assert gemini.AGENT.install.binary == "gemini"
        assert gemini.AGENT.install.package == "@google/gemini-cli"
        assert gemini.AGENT.install.version_error is None
        assert gemini.AGENT.install.upgrade_argv is None

    def test_too_new_follows_the_downgrade_check(self, monkeypatch):
        monkeypatch.setattr(gemini, "too_new_downgrade", lambda: ("0.45.0", "0.44.1"))
        assert gemini.AGENT.install.too_new is not None
        assert gemini.AGENT.install.too_new() == ("0.45.0", "0.44.1")


class TestGeminiAgentModels:
    def test_pins_the_first_discovered_model(self):
        models = gemini.AGENT.models({"gemini_models": ["gemini-3", "gemini-2", "gemini-3"]})
        assert models.available == ("gemini-3", "gemini-2")
        assert models.default == "gemini-3"

    def test_empty_inventory_has_no_default(self):
        models = gemini.AGENT.models({})
        assert models.available == ()
        assert models.default is None

    def test_pinned_default_wins(self):
        state = {"gemini_models": ["gemini-3"], "gemini_default_model": "gemini-2"}
        assert gemini.AGENT.models(state).default == "gemini-2"

    def test_static_list_replaces_discovery(self):
        state = {"gemini_models": ["gemini-3"], "gemini_static_models": ["only-this"]}
        assert gemini.AGENT.models(state).available == ("only-this",)

    def test_does_not_mutate_state(self):
        state = {"gemini_models": ["gemini-3"]}
        gemini.AGENT.models(state)
        assert state == {"gemini_models": ["gemini-3"]}


class TestGeminiAgentConfigure:
    def test_returns_the_state_not_a_tuple(self, monkeypatch):
        calls: list[tuple] = []

        def fake_write(state, model, token=None, *, force_refresh=False, provider=None):
            calls.append((model, provider))
            return {**state, "written": True}, "tok"

        monkeypatch.setattr(gemini, "write_tool_config", fake_write)
        result = gemini.AGENT.configure({"workspace": WS}, ConfigureRequest(model="gemini-3"))
        assert result == {"workspace": WS, "written": True}
        assert calls == [("gemini-3", None)]

    def test_passes_the_provider_service_through(self, monkeypatch):
        calls: list[tuple] = []

        def fake_write(state, model, token=None, *, force_refresh=False, provider=None):
            calls.append((model, provider))
            return state, "tok"

        monkeypatch.setattr(gemini, "write_tool_config", fake_write)
        gemini.AGENT.configure(
            {"workspace": WS}, ConfigureRequest(model="gemini-3.5-flash", provider="c.s.svc")
        )
        assert calls == [("gemini-3.5-flash", "c.s.svc")]

    def test_requires_a_model_even_under_a_provider(self):
        with pytest.raises(RuntimeError, match="gemini model must be selected"):
            gemini.AGENT.configure({"workspace": WS}, ConfigureRequest(provider="c.s.svc"))

    def test_writes_the_env_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gemini, "GEMINI_ENV_PATH", tmp_path / "ucode.env")
        monkeypatch.setattr(gemini, "GEMINI_BACKUP_PATH", tmp_path / "backup")
        monkeypatch.setattr(gemini, "save_state", lambda s: None)
        monkeypatch.setattr(gemini, "get_databricks_token", lambda *a, **k: "fake-token")

        state = gemini.AGENT.configure({"workspace": WS}, ConfigureRequest(model="some-model"))

        assert isinstance(state, dict)
        assert 'GEMINI_MODEL="some-model"' in (tmp_path / "ucode.env").read_text()


class TestGeminiAgentRevert:
    def test_restores_the_env_file(self, monkeypatch):
        calls: list[tuple] = []

        def fake_restore(config, backup, managed):
            calls.append((config, backup, managed))
            return True

        monkeypatch.setattr(gemini, "restore_file", fake_restore)
        rows = gemini.AGENT.revert({"managed_configs": {"gemini": True}})
        assert rows == [("Gemini CLI config", "restored")]
        assert calls == [(gemini.GEMINI_ENV_PATH, gemini.GEMINI_BACKUP_PATH, True)]

    def test_reports_unchanged_when_nothing_to_restore(self, monkeypatch):
        monkeypatch.setattr(gemini, "restore_file", lambda *_a: False)
        assert gemini.AGENT.revert({}) == [("Gemini CLI config", "unchanged")]


class TestGeminiMcpClient:
    def _capture(self, monkeypatch, returncode=0, stdout="", stderr=""):
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(gemini.subprocess_cross_os, "run", fake_run)
        return calls

    def test_has_no_oauth_client(self):
        assert gemini.AGENT.mcp.oauth_client_id is None
        assert gemini.AGENT.mcp.binary == "gemini"
        assert gemini.AGENT.mcp.display == "Gemini CLI"

    def test_add_registers_the_stdio_proxy_in_the_pinned_home(self, monkeypatch):
        calls = self._capture(monkeypatch, returncode=1, stderr="Server not found")

        removed = gemini.AGENT.mcp.add("github", _server())

        assert removed == []  # nothing was registered before
        remove_call, add_call = calls
        assert remove_call["args"] == ["gemini", "mcp", "remove", "github", "--scope", "user"]
        args = add_call["args"]
        assert args[:4] == ["gemini", "mcp", "add", "github"]
        assert args[4 : 4 + len(PROXY_ARGV)] == list(PROXY_ARGV)
        assert args[-4:] == ["--type", "stdio", "--scope", "user"]
        # GEMINI_CLI_HOME must point at the launcher's home so `gemini mcp add` writes the same
        # settings.json the ug session reads from.
        for call in calls:
            assert call["kwargs"]["env"]["GEMINI_CLI_HOME"] == str(gemini.GEMINI_HOME_DIR)

    def test_add_reports_the_scope_an_existing_entry_was_removed_from(self, monkeypatch):
        self._capture(monkeypatch)
        assert gemini.AGENT.mcp.add("github", _server()) == ["user"]

    def test_add_ignores_an_oauth_client_and_still_proxies(self, monkeypatch):
        calls = self._capture(monkeypatch, returncode=1, stderr="Server not found")
        gemini.AGENT.mcp.add("github", _server(oauth_client="app-id"))
        assert "--type" in calls[-1]["args"] and "stdio" in calls[-1]["args"]

    def test_add_failure_is_an_actionable_error(self, monkeypatch):
        def boom(args, **kwargs):
            if args[2] == "remove":
                return MagicMock(returncode=1, stdout="", stderr="Server not found")
            raise subprocess.CalledProcessError(1, args)

        monkeypatch.setattr(gemini.subprocess_cross_os, "run", boom)
        with pytest.raises(RuntimeError, match="Failed to add MCP server 'github' via gemini CLI"):
            gemini.AGENT.mcp.add("github", _server())

    def test_remove_reports_user_scope_when_present(self, monkeypatch):
        calls = self._capture(monkeypatch)
        assert gemini.AGENT.mcp.remove("github") == ["user"]
        assert calls[0]["kwargs"]["env"]["GEMINI_CLI_HOME"] == str(gemini.GEMINI_HOME_DIR)

    def test_remove_of_a_missing_server_is_empty(self, monkeypatch):
        self._capture(monkeypatch, returncode=1, stderr="Server not found")
        assert gemini.AGENT.mcp.remove("github") == []

    def test_remove_failure_raises(self, monkeypatch):
        self._capture(monkeypatch, returncode=2, stderr="boom")
        with pytest.raises(RuntimeError, match="Failed to remove MCP server"):
            gemini.AGENT.mcp.remove("github")

    def test_remove_timeout_raises(self, monkeypatch):
        def slow(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 30)

        monkeypatch.setattr(gemini.subprocess_cross_os, "run", slow)
        with pytest.raises(RuntimeError, match="Timed out removing MCP server"):
            gemini.AGENT.mcp.remove("github")

    def test_entry_matches_what_the_cli_writes(self):
        assert gemini.AGENT.mcp.entry(_server()) == {
            "command": "/usr/bin/ug",
            "args": list(PROXY_ARGV[1:]),
        }

    def test_apply_batches_one_settings_write(self):
        path = gemini.GEMINI_SETTINGS_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"mcpServers": {"old": {"command": "x"}}, "theme": "dark"}))

        removed = gemini.AGENT.mcp.apply({"svc": _server()}, {"old"})

        assert removed == {"old"}
        doc = json.loads(path.read_text())
        assert doc["theme"] == "dark"
        assert doc["mcpServers"] == {"svc": gemini.AGENT.mcp.entry(_server())}

    def test_live_status_lists_in_the_pinned_home(self, monkeypatch):
        calls = self._capture(monkeypatch, stdout="github: cmd - ✔ Connected\n")
        assert gemini.AGENT.mcp.live_status() == {"github": mcp.LIVE_CONNECTED}
        assert calls[0]["args"] == ["gemini", "mcp", "list"]
        assert calls[0]["kwargs"]["env"]["GEMINI_CLI_HOME"] == str(gemini.GEMINI_HOME_DIR)

    def test_live_status_is_empty_when_the_listing_fails(self, monkeypatch):
        def missing(args, **kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(gemini.subprocess_cross_os, "run", missing)
        assert gemini.AGENT.mcp.live_status() == {}
