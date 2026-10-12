"""Tests for agents/copilot.py."""

from __future__ import annotations

import json
import threading

import pytest

from ucode.agents import copilot

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
        env = copilot.render_env_overlay(WS, "gpt-5", "tok")
        assert env["COPILOT_PROVIDER_BASE_URL"] == f"{WS}/ai-gateway/mlflow/v1"

    def test_claude_uses_anthropic_provider_with_catalog_model_id(self):
        env = copilot.render_env_overlay(WS, "system.ai.claude-sonnet-5-5", "tok")

        assert env["COPILOT_PROVIDER_TYPE"] == "anthropic"
        assert env["COPILOT_PROVIDER_BASE_URL"] == f"{WS}/ai-gateway/anthropic"
        assert env["COPILOT_PROVIDER_MODEL_ID"] == "claude-sonnet-5.5"
        assert env["COPILOT_MODEL"] == "system.ai.claude-sonnet-5-5"
        assert "COPILOT_PROVIDER_WIRE_API" not in env

    def test_claude_wire_model_override_selects_anthropic_provider(self):
        env = copilot.render_env_overlay(
            WS, "gpt-5", "tok", override_model="system.ai.claude-opus-4-7"
        )

        assert env["COPILOT_PROVIDER_TYPE"] == "anthropic"
        assert env["COPILOT_PROVIDER_MODEL_ID"] == "claude-opus-4.7"

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
            ("system.ai.gpt-6-astra", "gpt-4.1", "completions"),
            ("system.ai.gpt-5-6-sol", "databricks-gpt-6-1-sol", "responses"),
            ("system.ai.gpt-6-astra", "", "responses"),
            ("gpt-5", "", "responses"),
            ("gpt-4.1", "", "completions"),
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
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_MODEL", "gpt-4.1")
        monkeypatch.setenv("COPILOT_PROVIDER_WIRE_API", "responses")
        monkeypatch.setenv("COPILOT_PROVIDER_MODEL_ID", "user-model-id")
        monkeypatch.setenv("COPILOT_PROVIDER_MODEL_LIMITS_ID", "user-model-limits")

        env = copilot.build_runtime_env(WS, "system.ai.gpt-6-astra", "tok")

        assert env["COPILOT_PROVIDER_WIRE_MODEL"] == "gpt-4.1"
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


class TestCopilotCatalogModelId:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("system.ai.claude-sonnet-5-5", "claude-sonnet-5.5"),
            ("system.ai.claude-opus-5-5[1m]", "claude-opus-5.5"),
            ("system.ai.claude-opus-4-7", "claude-opus-4.7"),
            ("system.ai.claude-sonnet-5", "claude-sonnet-5"),
            ("system.ai.claude-haiku-4-5", "claude-haiku-4.5"),
            ("system.ai.claude-haiku-5-5", "claude-sonnet-5.5"),
            ("system.ai.claude-haiku-6", "claude-sonnet-6"),
        ],
    )
    def test_maps_claude_ids(self, model, expected):
        assert copilot.copilot_catalog_model_id(model) == expected

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5",
            "system.ai.gpt-6-astra",
            "claude-sonnet-4-6",
            "databricks-claude-opus-4-7",
            "global.anthropic.claude-opus-4-8",
            "main.schema.my-claude-finetune",
            "system.ai.myclaude-model",
        ],
    )
    def test_non_system_claude_ids_return_none(self, model):
        assert copilot.copilot_catalog_model_id(model) is None


class TestModelUsesResponsesApi:
    def test_numeric_gpt_majors_and_gateway_aliases_use_responses(self):
        for model in (
            "gpt-5",
            "databricks-gpt-5-mini",
            "system.ai.gpt-5-4",
            "gpt-6",
            "gpt-6.1-sol",
            "system.ai.gpt-6-astra",
            "databricks-gpt-6-1-sol",
            "gpt-7",
            "gpt-10",
        ):
            assert copilot.model_uses_responses_api(model), model

    def test_pre_gpt5_non_gpt_and_unsupported_alias_shapes_use_completions(self):
        for model in (
            "gpt-4.1",
            "gpt-4o",
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
        assert "COPILOT_PROVIDER_MODEL_ID" not in written
        assert written["COPILOT_PROVIDER_MODEL_LIMITS_ID"] == "user-model-limits"
        assert written["USER_SETTING"] == "keep"
        assert "COPILOT_PROVIDER_WIRE_API" in state["managed_configs"]["copilot"]["keys"]
        assert "COPILOT_PROVIDER_WIRE_MODEL" not in state["managed_configs"]["copilot"]["keys"]

    def test_claude_drops_stale_wire_api_from_env_file(self, tmp_path, monkeypatch):
        env_path = isolate_copilot_config_paths(monkeypatch, tmp_path)
        env_path.parent.mkdir(parents=True)
        env_path.write_text("COPILOT_PROVIDER_WIRE_API=completions\n", encoding="utf-8")

        copilot.write_tool_config({"workspace": WS}, "system.ai.claude-sonnet-5-5", token="tok")

        written = copilot.parse_dotenv(env_path)
        assert "COPILOT_PROVIDER_WIRE_API" not in written
        assert written["COPILOT_PROVIDER_TYPE"] == "anthropic"
        assert written["COPILOT_PROVIDER_MODEL_ID"] == "claude-sonnet-5.5"


class TestLaunch:
    @pytest.mark.parametrize(
        ("tool_args", "pinned_model", "default", "expected_model", "expected_api"),
        [
            (["--model", "gpt-6.1-sol"], "gpt-5", "gpt-5", "gpt-6.1-sol", "responses"),
            ([], "system.ai.gpt-4-1", "gpt-6", "system.ai.gpt-4-1", "completions"),
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
        state = {"workspace": WS, "copilot_default_model": default}

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
        assert state["copilot_default_model"] == default

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
                {"workspace": WS, "copilot_default_model": "gpt-5"},
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


class TestManagedModels:
    def test_managed_models_win_over_the_shared_discovery_lists(self):
        state = {
            "copilot_models": ["system.ai.gpt-5"],
            "claude_models": {"sonnet": "shared-should-not-win"},
        }
        assert copilot.default_model(state) == "system.ai.gpt-5"

    def test_falls_back_to_the_shared_lists_without_a_managed_config(self):
        assert copilot.default_model({"claude_models": {"sonnet": "discovered"}}) == "discovered"

    def test_copilot_default_model_wins_over_allowlist(self):
        state = {
            "copilot_default_model": "admin-chosen-default",
            "copilot_models": ["system.ai.gpt-5"],
        }
        assert copilot.default_model(state) == "admin-chosen-default"


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
