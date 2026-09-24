"""Tests for agents/opencode.py."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ucode.agents import opencode
from ucode.agents.args import LaunchOptions
from ucode.state import load_state

WS = "https://example.databricks.com"


def _base_urls() -> dict[str, str]:
    return {
        "anthropic": f"{WS}/ai-gateway/anthropic/v1",
        "gemini": f"{WS}/ai-gateway/gemini/v1beta",
        "oss": f"{WS}/ai-gateway/mlflow/v1",
    }


def _model_state() -> dict:
    return {
        "workspace": WS,
        "profile": "test-profile",
        "opencode_default_model": "system.ai.claude-sonnet-4-6",
        "opencode_models": {
            "anthropic": ["system.ai.claude-sonnet-4-6"],
            "gemini": ["system.ai.gemini-3-flash"],
            "oss": ["system.ai.glm-5-2"],
        },
    }


@pytest.fixture
def opencode_config(tmp_path, monkeypatch):
    path = tmp_path / "opencode.json"
    monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", path)
    monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "opencode-backup.json")
    monkeypatch.setattr(opencode, "OPENCODE_XDG_CONFIG_HOME", tmp_path)
    monkeypatch.setattr(opencode, "get_databricks_token", lambda *a, **kw: "test-token")
    monkeypatch.setattr(opencode, "ug_version", lambda: "0.1.0")
    monkeypatch.setattr(opencode, "agent_version", lambda _binary: "1.0.220")
    return path


class TestOpencodeSpec:
    def test_binary(self):
        assert opencode.SPEC["binary"] == "opencode"

    def test_package(self):
        assert opencode.SPEC["package"] == "opencode-ai@1"

    def test_display(self):
        assert opencode.SPEC["display"] == "OpenCode"

    def test_config_path_is_under_ucode_xdg_home(self):
        assert opencode.SPEC["config_path"] == (
            opencode.OPENCODE_XDG_CONFIG_HOME / "opencode" / "opencode.json"
        )

    def test_requires_version_with_custom_provider_fetch(self, monkeypatch):
        monkeypatch.setattr(opencode, "agent_version", lambda _binary: "1.0.219")

        message = opencode.minimum_version_error()

        assert message is not None
        assert "requires OpenCode 1.0.220 or newer" in message
        assert "npm install -g opencode-ai@1" in message

    def test_supported_version_needs_no_required_update(self, monkeypatch):
        monkeypatch.setattr(opencode, "agent_version", lambda _binary: "1.0.220")

        assert opencode.minimum_version_error() is None


class TestAuthPlugin:
    def test_calls_cross_platform_auth_token_helper_only_when_refreshing(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/opt/{command}")

        plugin = opencode.render_auth_plugin({"workspace": WS, "profile": "my profile"})

        assert (
            'const AUTH_COMMAND = ["/opt/ug", "auth-token", "--host", '
            f'"{WS}", "--profile", "my profile", "--force-refresh"]'
        ) in plugin
        assert "run(AUTH_COMMAND[0], AUTH_COMMAND.slice(1)" in plugin
        assert '"chat.headers"' not in plugin

    def test_installs_cached_refreshing_fetch_on_databricks_providers(self):
        plugin = opencode.render_auth_plugin({"workspace": WS})

        assert "config: async (config)" in plugin
        assert "options.fetch = databricksFetch" in plugin
        assert "expiresAt <= Date.now() + REFRESH_SKEW_MS" in plugin
        assert 'headers.set("Authorization", "Bearer " + token)' in plugin
        assert "if (response.status !== 401) return response" in plugin
        assert "return fetch(input, requestWithToken(input, init, accessToken))" in plugin

    def test_refresh_is_single_flighted(self):
        plugin = opencode.render_auth_plugin({"workspace": WS})

        assert "if (!refreshPromise)" in plugin
        assert "mintToken().finally(() => { refreshPromise = undefined })" in plugin


class TestRenderOverlay:
    def test_sets_model(self):
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), {})
        assert overlay["model"] == "claude-sonnet"

    def test_anthropic_provider_added_when_models_present(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": []}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert "databricks-anthropic" in overlay["provider"]

    def test_gemini_provider_added_when_models_present(self):
        models = {"anthropic": [], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert "databricks-google" in overlay["provider"]

    def test_oss_provider_added_when_models_present(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert "databricks-oss" in overlay["provider"]

    def test_oss_provider_uses_ai_sdk_openai_package(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert overlay["provider"]["databricks-oss"]["npm"] == "@ai-sdk/openai"

    @pytest.mark.parametrize(
        ("bucket", "model"),
        [("openai", "system.ai.gpt-5"), ("oss", "system.ai.gpt-oss-120b")],
    )
    def test_mlflow_provider_configuration(self, bucket, model):
        overlay, keys = opencode.render_overlay(model, "tok", _base_urls(), {bucket: [model]})

        provider_id = f"databricks-{bucket}"
        assert set(overlay["provider"]) == {provider_id}
        provider = overlay["provider"][provider_id]
        assert overlay["model"] == f"{provider_id}/{model}"
        assert provider["npm"] == "@ai-sdk/openai"
        assert provider["options"]["baseURL"] == _base_urls()["oss"]
        assert set(provider["models"]) == {model}
        assert ["provider", provider_id] in keys

    def test_deepseek_uses_oss_provider(self):
        model = "system.ai.deepseek-v4-pro"

        overlay, _ = opencode.render_overlay(model, "tok", _base_urls(), {"oss": [model]})

        assert overlay["model"] == f"databricks-oss/{model}"
        assert model in overlay["provider"]["databricks-oss"]["models"]

    def test_both_providers_when_both_present(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert "databricks-anthropic" in overlay["provider"]
        assert "databricks-google" in overlay["provider"]

    def test_no_provider_key_when_no_models(self):
        overlay, _ = opencode.render_overlay("model", "tok", _base_urls(), {})
        assert "provider" not in overlay

    def test_anthropic_base_url(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        options = overlay["provider"]["databricks-anthropic"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/anthropic/v1"

    def test_gemini_base_url(self):
        models = {"gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        options = overlay["provider"]["databricks-google"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/gemini/v1beta"

    def test_oss_base_url(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        options = overlay["provider"]["databricks-oss"]["options"]
        assert options["baseURL"] == f"{WS}/ai-gateway/mlflow/v1"

    def test_glm_gets_token_limits(self):
        models = {"oss": ["system.ai.glm-5-2"]}
        overlay, _ = opencode.render_overlay("system.ai.glm-5-2", "tok", _base_urls(), models)
        glm = overlay["provider"]["databricks-oss"]["models"]["system.ai.glm-5-2"]
        # OpenCode's schema requires both context and output on `limit`.
        assert glm["limit"] == {"context": 200000, "output": 25000}

    def test_unknown_oss_model_has_no_output_cap(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        kimi = overlay["provider"]["databricks-oss"]["models"]["system.ai.kimi-k2-7-code"]
        assert "limit" not in kimi

    @pytest.mark.parametrize(
        ("bucket", "responses", "chat"),
        [
            ("oss", "system.ai.qwen35-122b-a10b", "system.ai.grok-4-6"),
            ("openai", "system.ai.gpt-5", "system.ai.gpt-5-chat"),
        ],
    )
    def test_discovered_models_choose_sdk_from_listing_metadata(self, bucket, responses, chat):
        api_types = {
            responses: ["mlflow/v1/chat/completions", "mlflow/v1/responses"],
            chat: ["mlflow/v1/chat/completions", "openai/v1/responses"],
        }
        overlay, _ = opencode.render_overlay(
            f"databricks-{bucket}/{responses}",
            "tok",
            _base_urls(),
            {bucket: [responses, chat]},
            model_api_types=api_types,
        )

        rendered = overlay["provider"][f"databricks-{bucket}"]["models"]
        assert rendered[responses]["provider"] == {"npm": "@ai-sdk/openai"}
        assert rendered[chat]["provider"] == {"npm": "@ai-sdk/openai-compatible"}

    def test_token_in_api_key(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "mytoken", _base_urls(), models)
        assert overlay["provider"]["databricks-anthropic"]["options"]["apiKey"] == "mytoken"

    def test_authorization_header(self):
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        headers = overlay["provider"]["databricks-anthropic"]["options"]["headers"]
        assert headers["Authorization"] == "Bearer tok"

    def test_anthropic_tool_streaming_disabled(self):
        # @ai-sdk/anthropic injects `eager_input_streaming: true` on tool defs,
        # which the Databricks gateway rejects. opencode's auto-disable skips
        # Claude models, so we opt out per-model. The setting must live in
        # `models.<m>.options` — per-call providerOptions — not provider options.
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        model_entry = overlay["provider"]["databricks-anthropic"]["models"]["claude-sonnet"]
        assert model_entry["options"]["toolStreaming"] is False

    def test_user_agent_header_anthropic(self, monkeypatch):
        # UA must live at the per-model level — OpenCode clobbers
        # provider-level `headers["User-Agent"]` in session/llm.ts.
        monkeypatch.setattr(opencode, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(opencode, "agent_version", lambda binary: "0.74.0")
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        model_headers = overlay["provider"]["databricks-anthropic"]["models"]["claude-sonnet"][
            "headers"
        ]
        assert model_headers["User-Agent"] == "ucode/0.1.0 opencode/0.74.0"

    def test_user_agent_header_gemini(self, monkeypatch):
        monkeypatch.setattr(opencode, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(opencode, "agent_version", lambda binary: "0.74.0")
        models = {"gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        model_headers = overlay["provider"]["databricks-google"]["models"]["gemini-2"]["headers"]
        assert model_headers["User-Agent"] == "ucode/0.1.0 opencode/0.74.0"

    def test_provider_level_headers_only_authorization(self, monkeypatch):
        # Sanity: provider-level headers should NOT include User-Agent (since
        # it's clobbered there) — only Authorization.
        models = {"anthropic": ["claude-sonnet"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        provider_headers = overlay["provider"]["databricks-anthropic"]["options"]["headers"]
        assert "User-Agent" not in provider_headers
        assert provider_headers["Authorization"] == "Bearer tok"

    def test_managed_keys_include_model(self):
        _, keys = opencode.render_overlay("model", "tok", _base_urls(), {})
        assert ["model"] in keys

    def test_managed_keys_include_anthropic_provider(self):
        models = {"anthropic": ["claude-sonnet"]}
        _, keys = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert ["provider", "databricks-anthropic"] in keys

    def test_managed_keys_include_gemini_provider(self):
        models = {"gemini": ["gemini-2"]}
        _, keys = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert ["provider", "databricks-google"] in keys

    def test_managed_keys_include_oss_provider(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        _, keys = opencode.render_overlay("system.ai.kimi-k2-7-code", "tok", _base_urls(), models)
        assert ["provider", "databricks-oss"] in keys

    def test_anthropic_models_listed(self):
        models = {"anthropic": ["claude-sonnet", "claude-haiku"]}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        provider_models = overlay["provider"]["databricks-anthropic"]["models"]
        assert "claude-sonnet" in provider_models
        assert "claude-haiku" in provider_models

    def test_prefixes_anthropic_model_with_provider_id(self):
        models = {"anthropic": ["claude-sonnet"], "gemini": []}
        overlay, _ = opencode.render_overlay("claude-sonnet", "tok", _base_urls(), models)
        assert overlay["model"] == "databricks-anthropic/claude-sonnet"

    def test_prefixes_gemini_model_with_provider_id(self):
        models = {"anthropic": [], "gemini": ["gemini-2"]}
        overlay, _ = opencode.render_overlay("gemini-2", "tok", _base_urls(), models)
        assert overlay["model"] == "databricks-google/gemini-2"

    def test_prefixes_oss_model_with_provider_id(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        assert overlay["model"] == "databricks-oss/system.ai.kimi-k2-7-code"

    @pytest.mark.parametrize(
        ("model", "npm", "limits"),
        [
            (
                "main.team.glm-custom",
                "@ai-sdk/openai-compatible",
                {"context": 200_000, "output": 25_000},
            ),
            (
                "system.ai.qwen35-122b-a10b",
                "@ai-sdk/openai",
                {"context": 262_144, "output": 25_000},
            ),
        ],
    )
    def test_requested_model_uses_selected_sdk_and_known_limits_without_changing_other_models(
        self, model, npm, limits
    ):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            f"databricks-oss/{model}",
            "tok",
            _base_urls(),
            models,
            requested_model=("databricks-oss", model, npm),
        )

        provider = overlay["provider"]["databricks-oss"]
        assert provider["npm"] == "@ai-sdk/openai"
        assert "provider" not in provider["models"]["system.ai.kimi-k2-7-code"]
        requested = provider["models"][model]
        assert requested["provider"] == {"npm": npm}
        assert requested["limit"] == limits
        assert models == {"oss": ["system.ai.kimi-k2-7-code"]}


class TestMcpServerConfig:
    # ucode registers the `ucode mcp-proxy ...` bridge as a `local` (stdio) MCP
    # server; the proxy handles token refresh, so no URL/bearer header here.
    PROXY_ARGV = ["ucode", "mcp-proxy", "--url", f"{WS}/api/2.0/mcp/functions/system/ai"]

    def test_builds_local_server_entry_from_proxy_argv(self):
        entry = opencode.build_mcp_server_entry(self.PROXY_ARGV)

        assert entry == {
            "type": "local",
            "command": self.PROXY_ARGV,
            "enabled": True,
        }

    def test_writes_mcp_server_without_clobbering_existing_config(self, tmp_path, monkeypatch):
        import ucode.agents.opencode as oc_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        config_file.write_text(
            json.dumps(
                {
                    "model": "existing-model",
                    "mcp": {"old-server": {"type": "local", "command": ["old"]}},
                }
            ),
            encoding="utf-8",
        )

        removed = oc_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        written = json.loads(config_file.read_text())
        assert removed is False
        assert written["model"] == "existing-model"
        assert written["mcp"]["old-server"] == {"type": "local", "command": ["old"]}
        assert written["mcp"]["github"] == {
            "type": "local",
            "command": self.PROXY_ARGV,
            "enabled": True,
        }

    def test_reports_replaced_mcp_server(self, tmp_path, monkeypatch):
        import ucode.agents.opencode as oc_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        config_file.write_text(json.dumps({"mcp": {"github": {"old": True}}}), encoding="utf-8")

        removed = oc_mod.write_mcp_server_config("github", self.PROXY_ARGV)

        assert removed is True
        written = json.loads(config_file.read_text())
        assert written["mcp"]["github"]["command"] == self.PROXY_ARGV

    def test_removes_mcp_server_without_clobbering_others(self, tmp_path, monkeypatch):
        import ucode.agents.opencode as oc_mod

        config_file = tmp_path / "opencode.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        config_file.write_text(
            json.dumps(
                {
                    "model": "existing-model",
                    "mcp": {
                        "github": {"url": "old"},
                        "jira": {"url": "keep"},
                    },
                }
            ),
            encoding="utf-8",
        )

        removed = oc_mod.remove_mcp_server_config("github")

        written = json.loads(config_file.read_text())
        assert removed is True
        assert "github" not in written["mcp"]
        assert written["mcp"]["jira"] == {"url": "keep"}
        assert written["model"] == "existing-model"


class TestBuildRuntimeEnv:
    def test_sets_oauth_token_for_mcp(self):
        env = opencode.build_runtime_env("tok")

        assert env["OAUTH_TOKEN"] == "tok"

    def test_sets_ucode_xdg_config_home(self):
        env = opencode.build_runtime_env("tok")

        assert env["XDG_CONFIG_HOME"] == str(opencode.OPENCODE_XDG_CONFIG_HOME)


class TestOpencodeDefaultModel:
    def test_prefers_anthropic(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]}}
        assert opencode.default_model(state) == "claude-sonnet"

    def test_falls_back_to_gemini(self):
        state = {"opencode_models": {"anthropic": [], "gemini": ["gemini-2"]}}
        assert opencode.default_model(state) == "gemini-2"

    def test_prefers_gemini_before_openai_and_oss(self):
        state = {
            "opencode_models": {
                "anthropic": [],
                "openai": ["system.ai.gpt-5"],
                "gemini": ["gemini-2"],
                "oss": ["system.ai.gpt-oss-120b"],
            }
        }
        assert opencode.default_model(state) == "gemini-2"

    def test_falls_back_to_oss(self):
        state = {
            "opencode_models": {
                "anthropic": [],
                "gemini": [],
                "oss": ["system.ai.kimi-k2-7-code"],
            }
        }
        assert opencode.default_model(state) == "system.ai.kimi-k2-7-code"

    def test_returns_none_when_empty(self):
        assert opencode.default_model({}) is None
        assert opencode.default_model({"opencode_models": {}}) is None

    def test_opencode_default_model_wins_over_bucketed_models(self):
        state = {
            "opencode_default_model": "admin-chosen-default",
            "opencode_models": {"anthropic": ["claude-sonnet"]},
        }
        assert opencode.default_model(state) == "admin-chosen-default"


class TestOpencodeValidateCmd:
    def test_starts_with_binary(self):
        cmd = opencode.validate_cmd("opencode")
        assert cmd[0] == "opencode"

    def test_uses_run_subcommand(self):
        cmd = opencode.validate_cmd("opencode")
        assert "run" in cmd

    def test_has_prompt(self):
        cmd = opencode.validate_cmd("opencode")
        assert len(cmd) > 2


class TestWriteToolConfigStaleProviderCleanup:
    def test_stale_providers_removed_before_merge(self, tmp_path, monkeypatch):
        import ucode.agents.opencode as oc_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        stale = {
            "provider": {
                "databricks-anthropic": {"old": True},
                "databricks-google": {"old": True},
                "databricks-openai": {"old": True},
                "other-provider": {"keep": True},
            }
        }
        config_file.write_text(json.dumps(stale), encoding="utf-8")

        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"]},
            "managed_configs": {},
        }

        with (
            patch("ucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("ucode.agents.opencode.save_state"),
        ):
            oc_mod.write_tool_config(state, "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        providers = written.get("provider", {})
        # stale entry is replaced with new data, not kept as-is
        assert providers.get("databricks-anthropic") != {"old": True}
        assert "databricks-openai" not in providers
        # unmanaged provider entry survives
        assert providers.get("other-provider") == {"keep": True}
        # OpenCode 1.0.0 discovers `plugin/`; plural `plugins/` came later.
        plugin = config_file.parent / "plugin" / opencode.OPENCODE_AUTH_PLUGIN_PATH.name
        assert plugin.exists()
        assert "options.fetch = databricksFetch" in plugin.read_text()

    def test_config_written_with_correct_model(self, tmp_path, monkeypatch):
        import ucode.agents.opencode as oc_mod
        import ucode.config_io as config_io_mod

        monkeypatch.setattr(config_io_mod, "APP_DIR", tmp_path)
        config_file = tmp_path / "opencode.json"
        backup_file = tmp_path / "opencode-backup.json"
        monkeypatch.setattr(oc_mod, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(oc_mod, "OPENCODE_BACKUP_PATH", backup_file)

        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"]},
            "managed_configs": {},
        }

        with (
            patch("ucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("ucode.agents.opencode.save_state"),
        ):
            oc_mod.write_tool_config(state, "claude-sonnet", token="tok")

        written = json.loads(config_file.read_text())
        assert written["model"] == "databricks-anthropic/claude-sonnet"


class TestWriteUserMcpServers:
    def test_batched_add_remove_preserves_other_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "opencode.json"
        path.write_text(
            json.dumps({"provider": {"p": 1}, "mcp": {"mine": {"type": "local"}, "gone": {}}})
        )
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", path)
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "backup.json")

        opencode.write_user_mcp_servers(
            {"svc": opencode.build_mcp_server_entry(["ug", "mcp-proxy", "u"])}, {"gone"}
        )

        doc = json.loads(path.read_text())
        assert doc["provider"] == {"p": 1}
        assert "gone" not in doc["mcp"]
        assert doc["mcp"]["mine"] == {"type": "local"}
        assert doc["mcp"]["svc"]["command"] == ["ug", "mcp-proxy", "u"]


class TestExtractModelArgs:
    @pytest.mark.parametrize(
        "flags",
        [["--model", "chosen"], ["--model=chosen"], ["-m", "chosen"], ["-mchosen"], ["-m=chosen"]],
    )
    def test_extracts_model_and_preserves_other_arguments(self, flags):
        tool_args = ["run", "--format", "json", *flags, "prompt"]

        assert opencode.extract_model_args(None, tool_args) == (
            "chosen",
            ["run", "--format", "json", "prompt"],
        )
        assert tool_args == ["run", "--format", "json", *flags, "prompt"]

    def test_identical_values_coalesce(self):
        assert opencode.extract_model_args(
            "chosen",
            ["--model", "chosen", "run", "--model=chosen", "-m", "chosen", "-mchosen", "-m=chosen"],
        ) == ("chosen", ["run"])

    def test_preserves_arguments_after_native_separator(self):
        assert opencode.extract_model_args(
            None,
            [
                "run",
                "-m",
                "chosen",
                "--",
                "--model",
                "literal",
                "-m",
                "also-literal",
                "-mattached",
                "-m=attached",
            ],
        ) == (
            "chosen",
            ["run", "--", "--model", "literal", "-m", "also-literal", "-mattached", "-m=attached"],
        )

    @pytest.mark.parametrize(
        ("model", "tool_args"),
        [
            ("", []),
            (" ", []),
            (None, ["--model"]),
            (None, ["-m"]),
            (None, ["-m="]),
            (None, ["--model", "--format", "json"]),
        ],
    )
    def test_rejects_missing_or_empty_value(self, model, tool_args):
        with pytest.raises(RuntimeError, match="requires a .*value"):
            opencode.extract_model_args(model, tool_args)

    @pytest.mark.parametrize(
        ("model", "tool_args"),
        [
            ("first", ["--model", "second"]),
            (None, ["--model=first", "-m", "second"]),
            (None, ["-mfirst", "-m=second"]),
            ("main.team.model", ["--model", "databricks-oss/main.team.model"]),
        ],
    )
    def test_rejects_conflicting_values(self, model, tool_args):
        with pytest.raises(RuntimeError, match="Conflicting OpenCode models"):
            opencode.extract_model_args(model, tool_args)


class TestExplicitModelConfig:
    @pytest.mark.parametrize(
        ("model_id", "provider"),
        [
            ("system.ai.claude-sonnet-4-6", "databricks-anthropic"),
            ("system.ai.gemini-3-flash", "databricks-google"),
            ("system.ai.glm-5-2", "databricks-oss"),
        ],
    )
    @pytest.mark.parametrize("qualified", [False, True])
    def test_known_models_use_their_configured_provider(
        self, opencode_config, monkeypatch, model_id, provider, qualified
    ):
        monkeypatch.setattr(
            "ucode.databricks._http_get_json", lambda *a, **kw: pytest.fail("unexpected lookup")
        )
        state = _model_state()
        expected = _model_state()

        returned, token = opencode.write_tool_config(
            state, f"{provider}/{model_id}" if qualified else model_id
        )

        written = json.loads(opencode_config.read_text())
        assert written["model"] == f"{provider}/{model_id}"
        assert model_id in written["provider"][provider]["models"]
        assert returned["opencode_models"] == expected["opencode_models"]
        assert returned["opencode_default_model"] == expected["opencode_default_model"]
        assert token == "test-token"

    @pytest.mark.parametrize("qualified", [False, True])
    def test_registers_only_requested_chat_model_and_preserves_user_config(
        self, opencode_config, monkeypatch, qualified
    ):
        model_id = "main.team.claude-custom"
        calls = []

        def get(url, token, **kwargs):
            calls.append((url, token))
            return {
                "name": f"model-services/{model_id}",
                "supported_api_types": ["mlflow/v1/chat/completions"],
            }, None

        monkeypatch.setattr("ucode.databricks._http_get_json", get)
        original = {
            "theme": "custom",
            "mcp": {"local": {"type": "local", "command": ["real-server"]}},
            "provider": {
                "user-provider": {"models": {"user-model": {"name": "User's model"}}},
                "databricks-oss": {"models": {"main.team.old-transient": {}}},
            },
        }
        opencode_config.write_text(json.dumps(original))
        state = _model_state()
        expected = _model_state()

        opencode.write_tool_config(state, f"databricks-oss/{model_id}" if qualified else model_id)

        written = json.loads(opencode_config.read_text())
        assert calls == [(f"{WS}/api/2.1/unity-catalog/model-services/{model_id}", "test-token")]
        assert written["model"] == f"databricks-oss/{model_id}"
        provider = written["provider"]["databricks-oss"]
        assert provider["options"]["baseURL"] == _base_urls()["oss"]
        assert provider["options"]["apiKey"] == "test-token"
        assert set(provider["models"]) == {"system.ai.glm-5-2", model_id}
        assert provider["models"][model_id] == {
            "headers": {"User-Agent": "ucode/0.1.0 opencode/1.0.220"},
            "provider": {"npm": "@ai-sdk/openai-compatible"},
        }
        assert written["provider"]["user-provider"] == original["provider"]["user-provider"]
        assert written["mcp"] == original["mcp"]
        assert written["theme"] == "custom"
        plugin = opencode_config.parent / "plugin" / opencode.OPENCODE_AUTH_PLUGIN_PATH.name
        assert '"databricks-oss"' in plugin.read_text()
        saved = load_state()
        assert state["opencode_models"] == saved["opencode_models"] == expected["opencode_models"]
        assert (
            state["opencode_default_model"]
            == saved["opencode_default_model"]
            == expected["opencode_default_model"]
        )

        # An ordinary later write reconstructs the catalog and uses its saved default.
        opencode.write_tool_config(state, opencode.default_model(state))
        regenerated = json.loads(opencode_config.read_text())
        assert regenerated["model"] == f"databricks-anthropic/{expected['opencode_default_model']}"
        assert set(regenerated["provider"]["databricks-oss"]["models"]) == {"system.ai.glm-5-2"}
        assert regenerated["provider"]["user-provider"] == original["provider"]["user-provider"]
        assert regenerated["mcp"] == original["mcp"]

    def test_prefers_responses_for_requested_model_that_advertises_both_apis(
        self, opencode_config, monkeypatch
    ):
        model_id = "system.ai.qwen35-122b-a10b"
        monkeypatch.setattr(
            "ucode.databricks._http_get_json",
            lambda *a, **kw: (
                {
                    "name": f"model-services/{model_id}",
                    "supported_api_types": [
                        "mlflow/v1/chat/completions",
                        "mlflow/v1/responses",
                    ],
                },
                None,
            ),
        )

        opencode.write_tool_config(_model_state(), model_id)

        written = json.loads(opencode_config.read_text())
        requested = written["provider"]["databricks-oss"]["models"][model_id]
        assert requested["provider"] == {"npm": "@ai-sdk/openai"}
        assert requested["limit"] == {"context": 262_144, "output": 25_000}

    @pytest.mark.parametrize(
        ("model_id", "provider", "api_types", "npm"),
        [
            (
                "main.team.gpt-5",
                "databricks-openai",
                ["mlflow/v1/responses"],
                "@ai-sdk/openai",
            ),
            (
                "main.team.gpt-oss-120b",
                "databricks-oss",
                ["mlflow/v1/chat/completions"],
                "@ai-sdk/openai-compatible",
            ),
            (
                "main.team.grok-4-6",
                "databricks-oss",
                ["mlflow/v1/chat/completions"],
                "@ai-sdk/openai-compatible",
            ),
        ],
    )
    def test_undiscovered_model_uses_expected_transient_provider_and_sdk(
        self, opencode_config, monkeypatch, model_id, provider, api_types, npm
    ):
        monkeypatch.setattr(
            "ucode.databricks._http_get_json",
            lambda *a, **kw: (
                {"name": f"model-services/{model_id}", "supported_api_types": api_types},
                None,
            ),
        )

        opencode.write_tool_config({"workspace": WS}, model_id)

        written = json.loads(opencode_config.read_text())
        assert written["model"] == f"{provider}/{model_id}"
        requested = written["provider"][provider]["models"][model_id]
        assert requested["provider"] == {"npm": npm}

    @pytest.mark.parametrize(
        "model",
        [
            "databricks-openai/main.team.custom-model",
            "databricks-openai/main.team.gpt-oss-120b",
            "databricks-oss/main.team.gpt-5",
        ],
    )
    def test_rejects_known_provider_when_model_belongs_to_other_mlflow_bucket(
        self, opencode_config, monkeypatch, model
    ):
        monkeypatch.setattr(
            "ucode.databricks._http_get_json", lambda *a, **kw: pytest.fail("unexpected lookup")
        )

        with pytest.raises(RuntimeError, match="uses a different provider"):
            opencode.write_tool_config({"workspace": WS}, model)

        assert not opencode_config.exists()

    def test_native_provider_is_left_for_opencode_to_validate(self, opencode_config, monkeypatch):
        monkeypatch.setattr(
            "ucode.databricks._http_get_json", lambda *a, **kw: pytest.fail("unexpected lookup")
        )

        opencode.write_tool_config({"workspace": WS}, "openrouter/anthropic/claude-sonnet")

        written = json.loads(opencode_config.read_text())
        assert written["model"] == "openrouter/anthropic/claude-sonnet"
        assert "provider" not in written
        assert "opencode_models" not in load_state()

    @pytest.mark.parametrize(
        "model",
        [
            "databricks-anthropic/system.ai.gemini-3-flash",
            "databricks-anthropic/system.ai.claude-new",
            "not-qualified",
            "main..model",
            "native/",
        ],
    )
    def test_rejects_invalid_selection_before_writing(self, opencode_config, monkeypatch, model):
        monkeypatch.setattr(
            "ucode.databricks._http_get_json", lambda *a, **kw: pytest.fail("unexpected lookup")
        )
        original = '{"model": "user/original", "theme": "custom"}'
        opencode_config.write_text(original)
        state = _model_state()
        expected = _model_state()

        with pytest.raises(RuntimeError):
            opencode.write_tool_config(state, model)

        assert opencode_config.read_text() == original
        assert not opencode.OPENCODE_BACKUP_PATH.exists()
        assert not (opencode_config.parent / "plugin").exists()
        assert state == expected
        assert load_state() == {}

    @pytest.mark.parametrize(
        "api_types",
        [
            None,
            "mlflow/v1/chat/completions",
            ["openai/v1/chat/completions"],
            ["mlflow/v1/chat/completions/extra"],
        ],
    )
    def test_requires_explicit_mlflow_generation_capability(
        self, opencode_config, monkeypatch, api_types
    ):
        payload = {"name": "model-services/main.team.new-model"}
        if api_types is not None:
            payload["supported_api_types"] = api_types
        monkeypatch.setattr("ucode.databricks._http_get_json", lambda *a, **kw: (payload, None))

        with pytest.raises(
            RuntimeError,
            match="does not advertise mlflow/v1/responses or mlflow/v1/chat/completions",
        ):
            opencode.write_tool_config({"workspace": WS}, "main.team.new-model")

        assert not opencode_config.exists()
        assert not (opencode_config.parent / "plugin").exists()
        assert load_state() == {}

    @pytest.mark.parametrize(
        "reason", ["HTTP 404 Not Found", "HTTP 403 Forbidden", "network error: timed out"]
    )
    def test_propagates_lookup_failure_without_writing(self, opencode_config, monkeypatch, reason):
        monkeypatch.setattr("ucode.databricks._http_get_json", lambda *a, **kw: (None, reason))

        with pytest.raises(RuntimeError, match=reason):
            opencode.write_tool_config({"workspace": WS}, "main.team.new-model")

        assert not opencode_config.exists()
        assert not (opencode_config.parent / "plugin").exists()
        assert load_state() == {}


class TestExplicitModelLaunch:
    def test_no_override_preserves_native_arguments(self, opencode_config, monkeypatch):
        tool_args = ["run", "prompt"]
        monkeypatch.setattr(
            "ucode.databricks._http_get_json", lambda *a, **kw: pytest.fail("unexpected lookup")
        )
        with patch.object(opencode.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            with pytest.raises(SystemExit) as exc:
                opencode.launch(_model_state(), tool_args, options=LaunchOptions())

        assert exc.value.code == 0
        assert popen.call_args.args[0] == ["opencode", *tool_args]
        assert (
            json.loads(opencode_config.read_text())["model"]
            == "databricks-anthropic/system.ai.claude-sonnet-4-6"
        )

    @pytest.mark.parametrize("empty_catalog", [False, True])
    def test_final_write_and_native_argv_keep_requested_model(
        self, opencode_config, monkeypatch, empty_catalog
    ):
        model_id = "main.team.new-model"
        monkeypatch.setattr(
            "ucode.databricks._http_get_json",
            lambda *a, **kw: (
                {
                    "name": f"model-services/{model_id}",
                    "supported_api_types": ["mlflow/v1/chat/completions"],
                },
                None,
            ),
        )
        state = {"workspace": WS} if empty_catalog else _model_state()
        expected = {} if empty_catalog else _model_state()
        # Bootstrap's first write must survive the launcher's second write.
        opencode.write_tool_config(state, model_id)
        tool_args = ["run", "--format", "json", "--", "--model", "literal-prompt"]

        with patch.object(opencode.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 7
            with pytest.raises(SystemExit) as exc:
                opencode.launch(state, tool_args, options=LaunchOptions(user_pinned_model=model_id))

        assert exc.value.code == 7
        assert popen.call_args.args[0] == [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            f"databricks-oss/{model_id}",
            "--",
            "--model",
            "literal-prompt",
        ]
        assert popen.call_args.kwargs["env"]["OAUTH_TOKEN"] == "test-token"
        assert popen.call_args.kwargs["env"]["XDG_CONFIG_HOME"] == str(opencode_config.parent)
        assert tool_args == ["run", "--format", "json", "--", "--model", "literal-prompt"]
        assert json.loads(opencode_config.read_text())["model"] == f"databricks-oss/{model_id}"
        assert state.get("opencode_models") == expected.get("opencode_models")
        assert state.get("opencode_default_model") == expected.get("opencode_default_model")

    def test_no_selection_and_no_default_fails_before_launch(self, opencode_config):
        with patch.object(opencode.subprocess, "Popen") as popen:
            with pytest.raises(RuntimeError, match="No OpenCode model is configured"):
                opencode.launch({"workspace": WS}, ["run", "prompt"], options=LaunchOptions())

        popen.assert_not_called()
        assert not opencode_config.exists()
