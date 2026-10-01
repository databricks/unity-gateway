"""Tests for agents/copilot.py."""

from __future__ import annotations

import json
import threading

import pytest

from ucode.agents import copilot
from ucode.agents.interface import ConfigureRequest, McpServer

WS = "https://example.databricks.com"


class TestCopilotSpec:
    def test_binary(self):
        assert copilot.SPEC["binary"] == "copilot"

    def test_package(self):
        assert copilot.SPEC["package"] == "@github/copilot"

    def test_display(self):
        assert copilot.SPEC["display"] == "GitHub Copilot CLI"

    def test_config_path_is_ucode_env_file(self):
        assert copilot.SPEC["config_path"].name == "ucode.env"


class TestRenderEnvOverlay:
    def test_sets_provider_base_url(self):
        env = copilot.render_env_overlay(WS, "claude-sonnet-4-6", "tok")
        assert env["COPILOT_PROVIDER_BASE_URL"] == f"{WS}/ai-gateway/mlflow/v1"

    def test_sets_provider_type(self):
        env = copilot.render_env_overlay(WS, "m", "t")
        assert env["COPILOT_PROVIDER_TYPE"] == "openai"

    def test_sets_model(self):
        env = copilot.render_env_overlay(WS, "claude-sonnet-4-6", "tok")
        assert env["COPILOT_MODEL"] == "claude-sonnet-4-6"

    def test_sets_bearer_token(self):
        env = copilot.render_env_overlay(WS, "m", "tok123")
        assert env["COPILOT_PROVIDER_BEARER_TOKEN"] == "tok123"

    def test_sets_offline_true(self):
        env = copilot.render_env_overlay(WS, "m", "t")
        assert env["COPILOT_OFFLINE"] == "true"

    @pytest.mark.parametrize(
        ("selected_model", "override_model", "expected_api"),
        [
            ("gpt-6.1-sol", None, "responses"),
            ("system.ai.gpt-6-astra", "gpt-5", "completions"),
            ("system.ai.gpt-5-6-sol", "databricks-gpt-6-1-sol", "responses"),
            ("system.ai.gpt-6-astra", "", "responses"),
            ("gpt-5", "", "completions"),
        ],
    )
    def test_selects_wire_api_from_override_and_keeps_selected_model(
        self, selected_model, override_model, expected_api
    ):
        env = copilot.render_env_overlay(WS, selected_model, "tok", override_model=override_model)

        assert env["COPILOT_PROVIDER_WIRE_API"] == expected_api
        assert env["COPILOT_MODEL"] == selected_model


class TestBuildRuntimeEnv:
    def test_inherits_path(self):
        env = copilot.build_runtime_env(WS, "m", "t")
        assert "PATH" in env

    def test_overrides_copilot_vars(self):
        env = copilot.build_runtime_env(WS, "m", "tok")
        assert env["COPILOT_PROVIDER_BASE_URL"] == f"{WS}/ai-gateway/mlflow/v1"
        assert env["COPILOT_PROVIDER_BEARER_TOKEN"] == "tok"

    def test_sets_oauth_token_for_mcp(self):
        env = copilot.build_runtime_env(WS, "m", "tok")
        assert env["OAUTH_TOKEN"] == "tok"

    def test_inherited_wire_model_overrides_route_without_being_cleared(self, monkeypatch):
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_MODEL", "gpt-5")
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_API", "responses")
        monkeypatch.setenv("COPILOT_PROVIDER_MODEL_ID", "user-model-id")
        monkeypatch.setenv("COPILOT_PROVIDER_MODEL_LIMITS_ID", "user-model-limits")

        env = copilot.build_runtime_env(WS, "system.ai.gpt-6-astra", "tok")

        assert env["COPILOT_PROVIDER_WIRE_MODEL"] == "gpt-5"
        assert env["COPILOT_PROVIDER_WIRE_API"] == "completions"
        assert env["COPILOT_PROVIDER_MODEL_ID"] == "user-model-id"
        assert env["COPILOT_PROVIDER_MODEL_LIMITS_ID"] == "user-model-limits"


class TestMcpServerConfig:
    # ucode registers the `ucode mcp-proxy ...` bridge as a `local` (stdio) MCP
    # server; the proxy refreshes the token, so no URL/bearer header here.
    PROXY_ARGV = ["ucode", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]

    def test_builds_local_server_entry_from_proxy_argv(self):
        entry = copilot.build_mcp_server_entry(self.PROXY_ARGV)

        assert entry == {
            "type": "local",
            "command": self.PROXY_ARGV[0],
            "args": self.PROXY_ARGV[1:],
            "tools": ["*"],
        }

    def test_writes_mcp_server_without_clobbering_existing_config(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "mcp-config.json"
        backup_file = tmp_path / "copilot-mcp-backup.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_BACKUP_PATH", backup_file)

        config_file.write_text(
            json.dumps(
                {
                    "other": True,
                    "mcpServers": {"old-server": {"type": "stdio", "command": "old"}},
                }
            ),
            encoding="utf-8",
        )

        removed = cp_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["other"] is True
        assert written["mcpServers"]["old-server"] == {"type": "stdio", "command": "old"}
        assert written["mcpServers"]["github"] == {
            "type": "local",
            "command": self.PROXY_ARGV[0],
            "args": self.PROXY_ARGV[1:],
            "tools": ["*"],
        }

    def test_reports_replaced_mcp_server(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "mcp-config.json"
        backup_file = tmp_path / "copilot-mcp-backup.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_BACKUP_PATH", backup_file)

        config_file.write_text(
            json.dumps({"mcpServers": {"github": {"old": True}}}),
            encoding="utf-8",
        )

        removed = cp_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        assert removed is True
        written = json.loads(config_file.read_text())
        assert written["mcpServers"]["github"]["command"] == self.PROXY_ARGV[0]

    def test_removes_mcp_server_without_clobbering_others(self, tmp_path, monkeypatch):
        import ucode.agents.copilot as cp_mod

        config_file = tmp_path / "mcp-config.json"
        monkeypatch.setattr(cp_mod, "COPILOT_MCP_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps(
                {
                    "other": True,
                    "mcpServers": {
                        "github": {"url": "old"},
                        "jira": {"url": "keep"},
                    },
                }
            ),
            encoding="utf-8",
        )

        removed = cp_mod.remove_mcp_server_config("github")

        written = json.loads(config_file.read_text())
        assert removed is True
        assert "github" not in written["mcpServers"]
        assert written["mcpServers"]["jira"] == {"url": "keep"}
        assert written["other"] is True


class TestDefaultModel:
    def test_prefers_claude_sonnet(self):
        state = {
            "claude_models": {"sonnet": "s4", "opus": "o4", "haiku": "h4"},
            "codex_models": ["gpt-5"],
        }
        assert copilot.default_model(state) == "s4"

    def test_falls_back_to_opus(self):
        state = {"claude_models": {"opus": "o4", "haiku": "h4"}}
        assert copilot.default_model(state) == "o4"

    def test_falls_back_to_haiku(self):
        state = {"claude_models": {"haiku": "h4"}}
        assert copilot.default_model(state) == "h4"

    def test_falls_back_to_codex_when_no_claude(self):
        state = {"codex_models": ["gpt-5", "gpt-5-mini"]}
        assert copilot.default_model(state) == "gpt-5"

    def test_returns_none_when_no_models(self):
        assert copilot.default_model({}) is None

    def test_ignores_gemini_models(self):
        # Gemini is excluded — Databricks' Gemini translator rejects copilot's request shape.
        state = {"gemini_models": ["gemini-2-5-pro"]}
        assert copilot.default_model(state) is None


class TestModelUsesResponsesApi:
    def test_numeric_gpt_majors_and_gateway_aliases_use_responses(self):
        for model in (
            "gpt-6",
            "gpt-6.1-sol",
            "system.ai.gpt-6-astra",
            "databricks-gpt-6-1-sol",
            "gpt-7",
            "gpt-10",
        ):
            assert copilot.model_uses_responses_api(model), model

    def test_older_non_gpt_and_unsupported_alias_shapes_use_completions(self):
        for model in (
            "gpt-5",
            "gpt-5.10-sol",
            "claude-sonnet-4-6",
            "my-gpt-6-model",
            "gpt6",
            "gpt.6",
            "gpt-60x",
        ):
            assert not copilot.model_uses_responses_api(model), model


def isolate_copilot_config_paths(monkeypatch, tmp_path):
    import ucode.config_io as config_io
    import ucode.state as state_mod

    app_dir = tmp_path / ".ucode"
    env_path = tmp_path / ".copilot" / "ucode.env"
    monkeypatch.setattr(config_io, "APP_DIR", app_dir)
    monkeypatch.setattr(state_mod, "APP_DIR", app_dir)
    monkeypatch.setattr(state_mod, "STATE_PATH", app_dir / "state.json")
    monkeypatch.setattr(copilot, "COPILOT_ENV_PATH", env_path)
    monkeypatch.setattr(copilot, "COPILOT_BACKUP_PATH", app_dir / "copilot-ucode-env.backup")
    monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", tmp_path / "missing-mcp.json")
    return env_path


class TestWriteToolConfig:
    def test_manages_wire_api_and_preserves_wire_model_and_user_values(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        env_path.parent.mkdir(parents=True)
        env_path.write_text(
            "COPILOT_PROVIDER_WIRE_MODEL=system.ai.gpt-6-1-sol\n"
            "COPILOT_PROVIDER_WIRE_API=completions\n"
            "COPILOT_PROVIDER_MODEL_ID=user-model-id\n"
            "COPILOT_PROVIDER_MODEL_LIMITS_ID=user-model-limits\n"
            "USER_SETTING=keep\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_MODEL", "gpt-5")

        state, token = copilot.write_tool_config(
            {"workspace": WS}, "system.ai.gpt-5-6-sol", token="tok"
        )

        written = copilot.parse_dotenv(env_path)
        assert token == "tok"
        assert written["COPILOT_MODEL"] == "system.ai.gpt-5-6-sol"
        assert written["COPILOT_PROVIDER_WIRE_API"] == "responses"
        assert written["COPILOT_PROVIDER_WIRE_MODEL"] == "system.ai.gpt-6-1-sol"
        assert written["COPILOT_PROVIDER_MODEL_ID"] == "user-model-id"
        assert written["COPILOT_PROVIDER_MODEL_LIMITS_ID"] == "user-model-limits"
        assert written["USER_SETTING"] == "keep"
        assert "COPILOT_PROVIDER_WIRE_API" in state["managed_configs"]["copilot"]["keys"]
        assert "COPILOT_PROVIDER_WIRE_MODEL" not in state["managed_configs"]["copilot"]["keys"]


class TestLaunch:
    @pytest.mark.parametrize(
        ("tool_args", "pinned_model", "default", "expected_model", "expected_api"),
        [
            (["--model", "gpt-6.1-sol"], "gpt-5", "gpt-5", "gpt-6.1-sol", "responses"),
            ([], "system.ai.gpt-5-6-sol", "gpt-6", "system.ai.gpt-5-6-sol", "completions"),
            ([], None, "system.ai.gpt-10", "system.ai.gpt-10", "responses"),
        ],
    )
    def test_launch_model_precedence_preserves_argv(
        self, tool_args, pinned_model, default, expected_model, expected_api, tmp_path, monkeypatch
    ):
        isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.delenv("COPILOT_PROVIDER_WIRE_MODEL", raising=False)
        monkeypatch.setattr(copilot, "get_databricks_token", lambda *args, **kwargs: "tok")
        monkeypatch.setattr(copilot, "TOKEN_REFRESH_INTERVAL_SECONDS", 3600)
        calls = []

        class Process:
            def wait(self):
                return 0

        def popen(argv, *, env):
            calls.append((argv, env))
            return Process()

        monkeypatch.setattr(copilot.subprocess_cross_os, "popen", popen)
        state = {"workspace": WS, "codex_models": [default]}

        with pytest.raises(SystemExit) as exit_info:
            copilot.launch(
                state,
                tool_args,
                options=copilot.LaunchOptions(user_pinned_model=pinned_model),
            )

        assert exit_info.value.code == 0
        argv, env = calls[0]
        assert argv == ["copilot", *tool_args]
        assert env["COPILOT_MODEL"] == expected_model
        assert env["COPILOT_PROVIDER_WIRE_API"] == expected_api
        assert state["codex_models"] == [default]

    def test_token_refresh_keeps_launch_model(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.delenv("COPILOT_PROVIDER_WIRE_MODEL", raising=False)
        monkeypatch.setattr(copilot, "TOKEN_REFRESH_INTERVAL_SECONDS", 0.01)
        token_calls = []
        refreshed = threading.Event()

        def get_token(*args, force_refresh=False, **kwargs):
            token_calls.append(force_refresh)
            if force_refresh:
                refreshed.set()
            return "tok"

        monkeypatch.setattr(copilot, "get_databricks_token", get_token)

        class Process:
            def wait(self):
                assert refreshed.wait(timeout=2)
                return 0

        monkeypatch.setattr(
            copilot.subprocess_cross_os,
            "popen",
            lambda *args, **kwargs: Process(),
        )

        with pytest.raises(SystemExit) as exit_info:
            copilot.launch(
                {"workspace": WS, "codex_models": ["gpt-5"]},
                [],
                options=copilot.LaunchOptions(user_pinned_model="gpt-6-astra"),
            )

        written = copilot.parse_dotenv(env_path)
        assert exit_info.value.code == 0
        assert token_calls[0] is False
        assert token_calls[1:]
        assert all(token_calls[1:])
        assert written["COPILOT_MODEL"] == "gpt-6-astra"
        assert written["COPILOT_PROVIDER_WIRE_API"] == "responses"


class TestManagedKeys:
    def test_includes_required_vars(self):
        for key in (
            "COPILOT_PROVIDER_TYPE",
            "COPILOT_PROVIDER_BASE_URL",
            "COPILOT_PROVIDER_WIRE_API",
            "COPILOT_MODEL",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_OFFLINE",
            "OAUTH_TOKEN",
        ):
            assert key in copilot.MANAGED_KEYS


class TestValidateCmd:
    def test_starts_with_binary(self):
        cmd = copilot.validate_cmd("copilot")
        assert cmd[0] == "copilot"

    def test_has_prompt_flag(self):
        cmd = copilot.validate_cmd("copilot")
        assert "--prompt" in cmd

    def test_adds_ucode_mcp_config_when_present(self, tmp_path, monkeypatch):
        mcp_path = tmp_path / "ucode-mcp-config.json"
        mcp_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", mcp_path)

        cmd = copilot.validate_cmd("copilot")

        assert cmd[:3] == ["copilot", "--additional-mcp-config", f"@{mcp_path}"]


class TestWriteUserMcpServers:
    def test_batched_add_remove_preserves_other_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "ucode-mcp-config.json"
        path.write_text(
            json.dumps({"other": 1, "mcpServers": {"mine": {"type": "local"}, "gone": {}}})
        )
        monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", path)
        monkeypatch.setattr(copilot, "COPILOT_MCP_BACKUP_PATH", tmp_path / "backup.json")

        copilot.write_user_mcp_servers(
            {"svc": copilot.build_mcp_server_entry(["ug", "mcp-proxy", "u"])}, {"gone"}
        )

        doc = json.loads(path.read_text())
        assert doc["other"] == 1
        assert "gone" not in doc["mcpServers"]
        assert doc["mcpServers"]["mine"] == {"type": "local"}
        assert doc["mcpServers"]["svc"]["command"] == "ug"


class TestAgentIdentity:
    def test_is_the_registered_copilot_agent(self):
        from ucode.agents import AGENTS

        assert AGENTS["copilot"] is copilot.AGENT

    def test_describes_itself_and_its_install(self):
        agent = copilot.AGENT

        assert agent.display == "GitHub Copilot CLI"
        assert agent.install.binary == "copilot"
        assert agent.install.package == "@github/copilot"
        # Copilot has no native updater, version floor or ceiling: npm installs and upgrades it.
        assert agent.install.upgrade_argv is None
        assert agent.install.version_error is None
        assert agent.install.too_new is None
        assert agent.install.before_install is None


class TestAgentModels:
    def test_falls_back_to_claude_then_codex_and_pins_the_first(self):
        models = copilot.AGENT.models(
            {
                "claude_models": {"sonnet": "s4", "opus": "o4"},
                "codex_models": ["gpt-5"],
                "gemini_models": ["g"],
            }
        )

        assert models.available == ("s4", "o4", "gpt-5")
        assert models.default == "s4"

    def test_dedupes_ids_present_in_both_inventories(self):
        state = {"claude_models": {"sonnet": "m"}, "codex_models": ["m", "other"]}

        assert copilot.AGENT.models(state).available == ("m", "other")

    def test_empty_inventory_has_no_default(self):
        models = copilot.AGENT.models({})

        assert models.available == ()
        assert models.default is None

    def test_static_list_replaces_discovery_and_explicit_default_wins(self):
        state = {
            "claude_models": {"sonnet": "s4"},
            "copilot_static_models": ["managed-a", "managed-b"],
            "copilot_default_model": "managed-b",
        }

        models = copilot.AGENT.models(state)

        assert models.available == ("managed-a", "managed-b")
        assert models.default == "managed-b"

    def test_does_not_mutate_state(self):
        state = {"claude_models": {"sonnet": "s4"}}

        copilot.AGENT.models(state)

        assert state == {"claude_models": {"sonnet": "s4"}}


class TestAgentConfigure:
    def test_requires_a_model(self):
        with pytest.raises(RuntimeError, match="A copilot model must be selected"):
            copilot.AGENT.configure({"workspace": WS}, ConfigureRequest())

    def test_returns_the_state_and_writes_the_env_file(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(copilot, "get_databricks_token", lambda *args, **kwargs: "tok")

        result = copilot.AGENT.configure(
            {"workspace": WS}, ConfigureRequest(model="claude-sonnet-4-6")
        )

        # Native configure returns the updated state itself, not the (state, token) write result.
        assert isinstance(result, dict)
        assert result["managed_configs"]["copilot"]["keys"] == copilot.MANAGED_KEYS
        written = copilot.parse_dotenv(env_path)
        assert written["COPILOT_MODEL"] == "claude-sonnet-4-6"
        assert written["COPILOT_PROVIDER_BEARER_TOKEN"] == "tok"


class TestAgentLaunch:
    def test_hands_state_args_and_options_to_launch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            copilot, "launch", lambda state, args, *, options: calls.append((state, args, options))
        )
        options = copilot.LaunchOptions(user_pinned_model="m")

        copilot.AGENT.launch({"workspace": WS}, ["--flag"], options=options)

        assert calls == [({"workspace": WS}, ["--flag"], options)]


class TestAgentRevert:
    def _point_spec_at(self, monkeypatch, tmp_path):
        config = tmp_path / "ucode.env"
        backup = tmp_path / "backup.env"
        monkeypatch.setitem(copilot.SPEC, "config_path", config)
        monkeypatch.setitem(copilot.SPEC, "backup_path", backup)
        return config, backup

    def test_restores_the_env_file_from_backup(self, tmp_path, monkeypatch):
        config, backup = self._point_spec_at(monkeypatch, tmp_path)
        config.write_text("COPILOT_MODEL=ug\n", encoding="utf-8")
        backup.write_text("USER=original\n", encoding="utf-8")

        rows = copilot.AGENT.revert({})

        assert rows == [("GitHub Copilot CLI config", "restored")]
        assert config.read_text(encoding="utf-8") == "USER=original\n"
        assert not backup.exists()

    def test_removes_a_ug_created_env_file_when_managed(self, tmp_path, monkeypatch):
        config, _ = self._point_spec_at(monkeypatch, tmp_path)
        config.write_text("COPILOT_MODEL=ug\n", encoding="utf-8")

        rows = copilot.AGENT.revert({"managed_configs": {"copilot": {"keys": ["COPILOT_MODEL"]}}})

        assert rows == [("GitHub Copilot CLI config", "restored")]
        assert not config.exists()

    def test_unmanaged_file_without_backup_is_unchanged(self, tmp_path, monkeypatch):
        config, _ = self._point_spec_at(monkeypatch, tmp_path)
        config.write_text("USER=mine\n", encoding="utf-8")

        rows = copilot.AGENT.revert({})

        assert rows == [("GitHub Copilot CLI config", "unchanged")]
        assert config.read_text(encoding="utf-8") == "USER=mine\n"


class TestAgentMcpClient:
    PROXY_ARGV = ("ug", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai")

    @pytest.fixture
    def mcp_paths(self, tmp_path, monkeypatch):
        import ucode.config_io as config_io

        monkeypatch.setattr(config_io, "APP_DIR", tmp_path)
        config = tmp_path / "ucode-mcp-config.json"
        backup = tmp_path / "mcp-backup.json"
        monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", config)
        monkeypatch.setattr(copilot, "COPILOT_MCP_BACKUP_PATH", backup)
        return config, backup

    def _server(self, **kwargs):
        return McpServer(url=f"{WS}/api/2.0/mcp/x", proxy_argv=self.PROXY_ARGV, **kwargs)

    def test_describes_itself_and_always_uses_the_proxy(self):
        client = copilot.AGENT.mcp

        assert client.display == "GitHub Copilot CLI"
        assert client.binary == "copilot"
        assert client.oauth_client_id is None

    def test_add_writes_the_proxy_entry_and_reports_replacement(self, mcp_paths):
        config, _ = mcp_paths
        client = copilot.AGENT.mcp

        assert client.add("github", self._server()) == []
        assert client.add("github", self._server()) == ["user"]

        assert json.loads(config.read_text())["mcpServers"]["github"] == {
            "type": "local",
            "command": "ug",
            "args": list(self.PROXY_ARGV[1:]),
            "tools": ["*"],
        }

    def test_add_ignores_a_native_oauth_client_and_always_load(self, mcp_paths):
        config, _ = mcp_paths

        copilot.AGENT.mcp.add("github", self._server(oauth_client="app-id", always_load=True))

        entry = json.loads(config.read_text())["mcpServers"]["github"]
        assert entry["command"] == "ug"
        assert "url" not in entry

    def test_remove_reports_the_user_scope_only_when_present(self, mcp_paths):
        config, _ = mcp_paths
        client = copilot.AGENT.mcp
        client.add("github", self._server())
        client.add("jira", self._server())

        assert client.remove("github") == ["user"]
        assert client.remove("github") == []

        assert list(json.loads(config.read_text())["mcpServers"]) == ["jira"]

    def test_apply_writes_a_whole_diff_and_returns_the_removed_names(self, mcp_paths):
        config, _ = mcp_paths
        config.write_text(
            json.dumps({"other": 1, "mcpServers": {"mine": {"type": "local"}, "gone": {}}})
        )
        client = copilot.AGENT.mcp

        removed = client.apply({"svc": self._server()}, {"gone", "never-there"})

        doc = json.loads(config.read_text())
        assert removed == {"gone"}
        assert doc["other"] == 1
        assert doc["mcpServers"]["mine"] == {"type": "local"}
        assert "gone" not in doc["mcpServers"]
        # The batched write records exactly what per-server `add` records.
        client.add("single", self._server())
        assert doc["mcpServers"]["svc"] == json.loads(config.read_text())["mcpServers"]["single"]

    def test_live_status_parses_the_mcp_list_output(self, monkeypatch):
        from ucode import mcp

        seen = []
        monkeypatch.setattr(
            mcp,
            "_read_mcp_listing",
            lambda argv, env=None: seen.append((argv, env)) or "github: ug mcp-proxy - ✓ Connected",
        )

        assert copilot.AGENT.mcp.live_status() == {"github": "connected"}
        assert seen == [(["copilot", "mcp", "list"], None)]

    def test_live_status_is_empty_when_the_listing_cannot_be_read(self, monkeypatch):
        from ucode import mcp

        monkeypatch.setattr(mcp, "_read_mcp_listing", lambda argv, env=None: None)

        assert copilot.AGENT.mcp.live_status() == {}


class TestRestoreMcpConfig:
    @pytest.fixture
    def mcp_paths(self, tmp_path, monkeypatch):
        config = tmp_path / "ucode-mcp-config.json"
        backup = tmp_path / "mcp-backup.json"
        monkeypatch.setattr(copilot, "COPILOT_MCP_CONFIG_PATH", config)
        monkeypatch.setattr(copilot, "COPILOT_MCP_BACKUP_PATH", backup)
        return config, backup

    def test_restores_the_original_file_and_drops_the_backup(self, mcp_paths):
        config, backup = mcp_paths
        config.write_text('{"mcpServers": {"ug": {}}}', encoding="utf-8")
        backup.write_text('{"mcpServers": {"mine": {}}}', encoding="utf-8")

        assert copilot.restore_mcp_config(False) is True

        assert json.loads(config.read_text()) == {"mcpServers": {"mine": {}}}
        assert not backup.exists()

    def test_deletes_a_ug_created_file_only_when_managed(self, mcp_paths):
        config, _ = mcp_paths
        config.write_text("{}", encoding="utf-8")

        assert copilot.restore_mcp_config(False) is False
        assert config.exists()
        assert copilot.restore_mcp_config(True) is True
        assert not config.exists()

    def test_revert_mcp_configs_restores_the_file_for_copilot_servers(self, mcp_paths, monkeypatch):
        from ucode import mcp

        config, backup = mcp_paths
        config.write_text('{"mcpServers": {"ug": {}}}', encoding="utf-8")
        backup.write_text('{"mcpServers": {"mine": {}}}', encoding="utf-8")
        monkeypatch.setattr(mcp, "remove_client_mcp_server", lambda client, name: [])

        results = mcp.revert_mcp_configs(
            {"mcp_servers": [{"name": "github", "clients": ["copilot"]}]}
        )

        assert results == {"copilot": True}
        assert json.loads(config.read_text()) == {"mcpServers": {"mine": {}}}
