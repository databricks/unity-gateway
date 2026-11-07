"""Tests for agents/gemini.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ucode import gateway_proxy
from ucode.agents import gemini
from ucode.agents.args import LaunchOptions
from ucode.telemetry import client_platform

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
        assert env["GEMINI_CLI_CUSTOM_HEADERS"] == (
            f"User-Agent:ucode/0.1.0 gemini/0.40.0 {client_platform()}"
        )

    def test_provider_adds_routing_header_and_pins_target(self, monkeypatch):
        monkeypatch.setattr(gemini, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.40.0")
        env = gemini.render_env_overlay(
            WS, "gemini-3.5-flash", "tok", provider="cat.sch.gemini-enterprise"
        )
        assert env["GEMINI_MODEL"] == "gemini-3.5-flash"
        assert env["GEMINI_CLI_CUSTOM_HEADERS"] == (
            f"User-Agent:ucode/0.1.0 gemini/0.40.0 {client_platform()},"
            "Databricks-Model-Provider-Service:cat.sch.gemini-enterprise"
        )

    def test_request_tags_header_not_yet_supported(self, monkeypatch):
        # TODO: Gemini request-tags support is deferred (GEMINI_CLI_CUSTOM_HEADERS
        # would shred a JSON value); for now the header is never emitted.
        monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", '{"team":"infra","env":"prod"}')
        env = gemini.render_env_overlay(WS, "gemini-2", "tok")
        assert "Databricks-Ai-Gateway-Request-Tags" not in env["GEMINI_CLI_CUSTOM_HEADERS"]


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

    def test_gemini_default_model_wins_over_allowlist(self):
        state = {
            "gemini_default_model": "admin-chosen-default",
            "gemini_models": ["gemini-2"],
        }
        assert gemini.default_model(state) == "admin-chosen-default"


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


class TestLaunch:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gemini, "GEMINI_ENV_PATH", tmp_path / "ucode.env")
        monkeypatch.setattr(gemini, "GEMINI_BACKUP_PATH", tmp_path / "backup")
        monkeypatch.setattr(gemini, "GEMINI_PROXY_LOG_PATH", tmp_path / "logs" / "proxy.log")
        monkeypatch.setattr(gemini, "save_state", lambda s: None)
        monkeypatch.setattr(gemini, "agent_version", lambda binary: "0.44.1")
        self.mints: list[tuple[str | None, bool]] = []

        def fake_token(workspace, profile=None, *, force_refresh=False):
            self.mints.append((profile, force_refresh))
            return f"dbx-token-{len(self.mints)}"

        monkeypatch.setattr(gemini, "get_databricks_token", fake_token)
        self.server, self.cache, self.client = MagicMock(), MagicMock(), MagicMock()
        self.server.server_address = ("127.0.0.1", 4321)
        self.proxy: dict = {}

        def fake_proxy(workspace, token_provider, **kwargs):
            self.proxy = {"workspace": workspace, "token_provider": token_provider, **kwargs}
            return self.server, self.cache, self.client

        monkeypatch.setattr(gateway_proxy, "start_gateway_auth_proxy", fake_proxy)

    def _launch(self, monkeypatch, state=None, *, wait=None):
        captured: dict = {}

        def fake_popen(argv, env):
            captured["argv"], captured["env"] = argv, env
            proc = MagicMock()
            proc.wait.side_effect = wait or (lambda: 0)
            return proc

        monkeypatch.setattr(gemini.subprocess_cross_os, "popen", fake_popen)
        state = state or {"workspace": WS, "profile": "dev", "gemini_models": ["gemini-2"]}
        with pytest.raises(SystemExit) as exc:
            gemini.launch(state, ["-p", "hi"], options=LaunchOptions())
        return captured, exc.value.code

    def test_gemini_talks_to_local_proxy_with_session_secret(self, monkeypatch):
        captured, code = self._launch(monkeypatch)

        assert code == 0
        assert captured["argv"] == ["gemini", "-p", "hi"]
        env = captured["env"]
        assert env["GOOGLE_GEMINI_BASE_URL"] == "http://127.0.0.1:4321/ai-gateway/gemini"
        assert env["GEMINI_API_KEY"] == self.proxy["session_secret"]
        assert not env["GEMINI_API_KEY"].startswith("dbx-token")
        assert env["GEMINI_API_KEY_AUTH_MECHANISM"] == "bearer"
        assert env["GEMINI_MODEL"] == "gemini-2"
        assert self.proxy["workspace"] == WS
        assert self.proxy["allowed_path_prefixes"] == ("/ai-gateway/gemini/",)

    def test_each_launch_gets_its_own_session_secret(self, monkeypatch):
        self._launch(monkeypatch)
        first = self.proxy["session_secret"]
        self._launch(monkeypatch)
        assert self.proxy["session_secret"] != first

    def test_proxy_mints_tokens_for_the_session_profile(self, monkeypatch):
        self._launch(monkeypatch)
        self.mints.clear()

        token = self.proxy["token_provider"](True)

        assert token == "dbx-token-1"
        assert self.mints == [("dev", True)]

    def test_env_file_keeps_the_databricks_token_not_the_secret(self, monkeypatch):
        self._launch(monkeypatch)

        written = gemini.parse_dotenv(gemini.GEMINI_ENV_PATH)
        assert written["GEMINI_API_KEY"] == "dbx-token-1"
        assert written["GOOGLE_GEMINI_BASE_URL"] == f"{WS}/ai-gateway/gemini"

    def test_provider_header_still_sent_by_gemini(self, monkeypatch):
        state = {
            "workspace": WS,
            "gemini_models": ["gemini-2"],
            "provider_services": {"gemini": "cat.sch.svc"},
        }
        gemini.GEMINI_ENV_PATH.write_text('GEMINI_MODEL="gemini-3.5-flash"\n')

        captured, _ = self._launch(monkeypatch, state)

        env = captured["env"]
        assert env["GEMINI_MODEL"] == "gemini-3.5-flash"
        assert "Databricks-Model-Provider-Service:cat.sch.svc" in env["GEMINI_CLI_CUSTOM_HEADERS"]

    def test_returns_gemini_exit_code_and_stops_proxy(self, monkeypatch):
        _, code = self._launch(monkeypatch, wait=lambda: 3)

        assert code == 3
        self.cache.stop.assert_called_once()
        self.server.shutdown.assert_called_once()
        self.server.server_close.assert_called_once()
        self.client.close.assert_called_once()

    def test_stops_proxy_when_gemini_fails_to_start(self, monkeypatch):
        monkeypatch.setattr(
            gemini.subprocess_cross_os,
            "popen",
            MagicMock(side_effect=FileNotFoundError("gemini")),
        )
        with pytest.raises(FileNotFoundError):
            gemini.launch(
                {"workspace": WS, "gemini_models": ["gemini-2"]}, [], options=LaunchOptions()
            )

        self.cache.stop.assert_called_once()
        self.server.shutdown.assert_called_once()
        self.client.close.assert_called_once()

    def test_ctrl_c_is_forwarded_and_proxy_stops(self, monkeypatch):
        results = iter([KeyboardInterrupt(), 130])

        def wait():
            result = next(results)
            if isinstance(result, BaseException):
                raise result
            return result

        _, code = self._launch(monkeypatch, wait=wait)

        assert code == 130
        self.server.shutdown.assert_called_once()

    def test_refresh_failure_is_logged_to_file(self):
        gemini._log_proxy_failure(RuntimeError("expired"))

        assert "token refresh failed: expired" in gemini.GEMINI_PROXY_LOG_PATH.read_text()
