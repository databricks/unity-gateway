"""Tests for agents/gemini.py."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock

import pytest

from ucode.agents import gemini
from ucode.agents.args import LaunchOptions
from ucode.state import set_provider_service
from ucode.telemetry import client_platform

WS = "https://example.databricks.com"
DEFAULT_MODEL = "system.ai.gemini-3-5-flash"
PRO_MODEL = "system.ai.gemini-3-1-pro"


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


class TestResolveExplicitModel:
    def test_returns_a_discovered_model(self):
        state = {"gemini_models": [DEFAULT_MODEL, PRO_MODEL]}

        assert gemini.resolve_explicit_model(PRO_MODEL, state) == PRO_MODEL

    def test_accepts_a_managed_default_outside_the_admin_model_list(self):
        state = {"gemini_default_model": "admin-chosen-default", "gemini_models": [PRO_MODEL]}

        assert (
            gemini.resolve_explicit_model("admin-chosen-default", state) == "admin-chosen-default"
        )
        with pytest.raises(RuntimeError, match=f"Available: {PRO_MODEL}, admin-chosen-default\\."):
            gemini.resolve_explicit_model("system.ai.no-such-model", state)

    @pytest.mark.parametrize(
        ("models", "available"),
        [([DEFAULT_MODEL, PRO_MODEL], f"{DEFAULT_MODEL}, {PRO_MODEL}"), ([], "none")],
        ids=["discovered", "nothing-discovered"],
    )
    def test_rejects_a_model_discovery_did_not_find(self, models, available):
        with pytest.raises(RuntimeError) as exc:
            gemini.resolve_explicit_model("system.ai.no-such-model", {"gemini_models": models})

        assert str(exc.value) == (
            "Gemini CLI model 'system.ai.no-such-model' is not configured. "
            f"Available: {available}. Run `ug configure --agent gemini` to rediscover models."
        )


class TestLaunch:
    @pytest.fixture(autouse=True)
    def _isolate_tool_config(self, tmp_path, monkeypatch):
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(gemini, "GEMINI_ENV_PATH", tmp_path / "ucode.env")
        monkeypatch.setattr(gemini, "GEMINI_BACKUP_PATH", tmp_path / "backup")
        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        monkeypatch.setattr("ucode.agents.gemini.save_state", lambda s: None)
        monkeypatch.setattr(
            "ucode.agents.gemini.get_databricks_token",
            lambda ws, profile=None, **kwargs: "fake-token",
        )

    @staticmethod
    def _state():
        return {"workspace": WS, "gemini_models": [DEFAULT_MODEL, PRO_MODEL]}

    @staticmethod
    def _launch(monkeypatch, state, tool_args=(), options=None):
        captured: dict = {"refresher_models": []}

        def fake_popen(argv, env):
            captured["argv"] = argv
            captured["env"] = env
            proc = MagicMock()
            proc.wait.return_value = 0
            return proc

        monkeypatch.setattr(gemini.subprocess_cross_os, "popen", fake_popen)
        # The real refresher sleeps 30 minutes between rewrites; record the model it was handed.
        monkeypatch.setattr(
            gemini,
            "_refresh_forever",
            lambda state, model, stop_event: captured["refresher_models"].append(model),
        )
        with pytest.raises(SystemExit) as exc:
            gemini.launch(state, list(tool_args), options=options or LaunchOptions())
        assert exc.value.code == 0
        return captured

    def test_ug_model_option_is_the_launch_model(self, monkeypatch):
        captured = self._launch(
            monkeypatch, self._state(), ["-p", "hi"], LaunchOptions(user_pinned_model=PRO_MODEL)
        )

        assert captured["env"]["GEMINI_MODEL"] == PRO_MODEL
        assert captured["argv"] == ["gemini", "-p", "hi"]
        assert gemini.persisted_provider_model() == PRO_MODEL  # the env file agrees
        assert captured["refresher_models"] == [PRO_MODEL]

    @pytest.mark.parametrize(
        "previous", [None, PRO_MODEL], ids=["first-launch", "after-model-launch"]
    )
    def test_without_a_model_option_launches_the_default(self, monkeypatch, previous):
        if previous:
            gemini.write_tool_config(self._state(), previous)  # an earlier `--model` launch

        captured = self._launch(monkeypatch, self._state())

        # `--model` is per launch: the env file a previous launch left must not make it stick.
        assert captured["env"]["GEMINI_MODEL"] == DEFAULT_MODEL
        assert gemini.persisted_provider_model() == DEFAULT_MODEL
        assert captured["refresher_models"] == [DEFAULT_MODEL]

    def test_bare_provider_relaunch_keeps_the_pinned_target(self, monkeypatch):
        provider = "cat.schema.svc"
        state = set_provider_service(self._state(), "gemini", provider)
        gemini.write_tool_config(state, "gemini-3.1-pro", provider=provider)  # an earlier launch

        captured = self._launch(monkeypatch, state)

        assert captured["env"]["GEMINI_MODEL"] == "gemini-3.1-pro"
        assert (
            f"Databricks-Model-Provider-Service:{provider}"
            in (captured["env"]["GEMINI_CLI_CUSTOM_HEADERS"])
        )
        assert captured["refresher_models"] == ["gemini-3.1-pro"]

    def test_token_refresh_keeps_rewriting_the_session_model(self, monkeypatch):
        written: list[str] = []
        stop_event = threading.Event()

        def fake_write(state, model, **kwargs):
            written.append(model)
            stop_event.set()
            return state, "fresh-token"

        monkeypatch.setattr(gemini, "TOKEN_REFRESH_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(gemini, "write_tool_config", fake_write)

        gemini._refresh_forever(self._state(), PRO_MODEL, stop_event)

        assert written == [PRO_MODEL]
