"""Tests for agents/opencode.py."""

from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from ucode.agents import opencode
from ucode.agents.args import LaunchOptions
from ucode.agents.interface import Agent, ConfigureRequest, McpClient, McpServer, Models
from ucode.constants import MCP_USER_SCOPE

WS = "https://example.databricks.com"


def _base_urls() -> dict[str, str]:
    return {
        "anthropic": f"{WS}/ai-gateway/anthropic/v1",
        "gemini": f"{WS}/ai-gateway/gemini/v1beta",
        "oss": f"{WS}/ai-gateway/mlflow/v1",
    }


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

    def test_non_glm_oss_model_has_no_output_cap(self):
        models = {"oss": ["system.ai.kimi-k2-7-code"]}
        overlay, _ = opencode.render_overlay(
            "system.ai.kimi-k2-7-code", "tok", _base_urls(), models
        )
        kimi = overlay["provider"]["databricks-oss"]["models"]["system.ai.kimi-k2-7-code"]
        assert "limit" not in kimi

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


class TestOpencodeLaunchModel:
    def test_resolves_configured_bare_and_native_selectors(self):
        state = {
            "opencode_models": {
                "anthropic": ["claude-sonnet"],
                "gemini": ["gemini-2"],
                "oss": ["system.ai.kimi-k2-7-code"],
            }
        }

        assert opencode.resolve_explicit_model("gemini-2", state) == "databricks-google/gemini-2"
        assert (
            opencode.resolve_explicit_model("openrouter/anthropic/claude-sonnet", state)
            == "openrouter/anthropic/claude-sonnet"
        )

    def test_rejects_unknown_and_mismatched_managed_selectors(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"]}}

        with pytest.raises(RuntimeError, match="not configured"):
            opencode.resolve_explicit_model("missing-model", state)
        with pytest.raises(RuntimeError, match="managed provider"):
            opencode.resolve_explicit_model("databricks-google/claude-sonnet", state)

    @pytest.mark.parametrize(
        ("model", "tool_args", "selector", "expected_args"),
        [
            (
                None,
                ["run", "prompt"],
                "databricks-anthropic/claude-sonnet",
                ["run", "prompt"],
            ),
            (
                "databricks-google/gemini-2",
                ["run", "--", "--model", "literal"],
                "databricks-google/gemini-2",
                ["run", "--model", "databricks-google/gemini-2", "--", "--model", "literal"],
            ),
            (
                "gemini-2",
                ["run", "prompt"],
                "databricks-google/gemini-2",
                ["run", "prompt", "--model", "databricks-google/gemini-2"],
            ),
            (
                "claude-sonnet",
                ["run", "--model", "databricks-google/gemini-2"],
                "databricks-google/gemini-2",
                ["run", "--model", "databricks-google/gemini-2"],
            ),
            (
                "openrouter/anthropic/claude-sonnet",
                ["run", "--model", "openrouter/anthropic/claude-sonnet"],
                "openrouter/anthropic/claude-sonnet",
                ["run", "--model", "openrouter/anthropic/claude-sonnet"],
            ),
        ],
    )
    def test_launch_preserves_selection_and_saved_defaults(
        self, tmp_path, monkeypatch, model, tool_args, selector, expected_args
    ):
        config_file = tmp_path / "opencode.json"
        monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", config_file)
        monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", tmp_path / "opencode-backup.json")
        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"], "gemini": ["gemini-2"]},
            "managed_configs": {},
        }
        original_state = deepcopy(state)
        original_args = list(tool_args)
        with (
            patch("ucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("ucode.agents.opencode.agent_version", return_value="1.0.220"),
            patch("ucode.agents.opencode.save_state"),
            patch("ucode.agents.opencode.subprocess_cross_os.popen") as popen,
        ):
            popen.return_value.wait.return_value = 7
            with pytest.raises(SystemExit) as exc_info:
                opencode.launch(state, tool_args, options=LaunchOptions(user_pinned_model=model))

        assert exc_info.value.code == 7
        assert json.loads(config_file.read_text())["model"] == selector
        assert popen.call_args.args[0] == ["opencode", *expected_args]
        assert tool_args == original_args
        assert state["opencode_models"] == original_state["opencode_models"]

    def test_rejects_invalid_model_before_configure_and_process(self):
        state = {"opencode_models": {"anthropic": ["claude-sonnet"]}}
        with (
            patch("ucode.agents.opencode._configure_launch") as configure,
            patch("ucode.agents.opencode.subprocess_cross_os.popen") as popen,
            pytest.raises(RuntimeError, match="not configured"),
        ):
            opencode.launch(
                state,
                ["run", "--model", "missing-model"],
                options=LaunchOptions(),
            )

        configure.assert_not_called()
        popen.assert_not_called()


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


def _server(*, oauth_client: str | None = None) -> McpServer:
    return McpServer(
        url=f"{WS}/api/2.0/mcp/external/github",
        proxy_argv=("ug", "mcp-proxy", "--url", "u"),
        oauth_client=oauth_client,
    )


@pytest.fixture
def oc_paths(tmp_path, monkeypatch):
    """Redirect OpenCode's config and backup so nothing touches the developer's machine."""
    config = tmp_path / "opencode.json"
    backup = tmp_path / "backup.json"
    monkeypatch.setattr(opencode, "OPENCODE_CONFIG_PATH", config)
    monkeypatch.setattr(opencode, "OPENCODE_BACKUP_PATH", backup)
    monkeypatch.setitem(opencode.SPEC, "config_path", config)
    monkeypatch.setitem(opencode.SPEC, "backup_path", backup)
    return config, backup


class TestOpenCodeAgent:
    def test_satisfies_the_agent_protocol(self):
        assert isinstance(opencode.AGENT, Agent)
        assert opencode.AGENT.display == "OpenCode"

    def test_install_describes_the_cli_and_its_minimum_version(self, monkeypatch):
        install = opencode.AGENT.install
        assert (install.binary, install.package) == ("opencode", "opencode-ai@1")
        assert install.upgrade_argv is None
        assert install.too_new is None
        assert install.before_install is None
        monkeypatch.setattr(opencode, "agent_version", lambda _binary: "1.0.219")
        assert install.version_error is not None
        assert "requires OpenCode 1.0.220 or newer" in (install.version_error() or "")

    def test_models_flatten_the_family_inventory_and_pin_the_first(self):
        state = {"opencode_models": {"anthropic": ["a", "b"], "gemini": ["g"], "oss": ["a"]}}
        assert opencode.AGENT.models(state) == Models(("a", "b", "g"), "a")

    def test_models_empty_without_discovery(self):
        assert opencode.AGENT.models({}) == Models((), None)

    def test_models_prefer_a_pinned_default_and_a_static_list(self):
        state = {
            "opencode_models": {"anthropic": ["a"]},
            "opencode_static_models": ["s1", "s2"],
            "opencode_default_model": "pinned",
        }
        assert opencode.AGENT.models(state) == Models(("s1", "s2"), "pinned")

    def test_models_does_not_mutate_state(self):
        state = {"opencode_models": {"anthropic": ["a"]}}
        before = deepcopy(state)
        opencode.AGENT.models(state)
        assert state == before

    def test_configure_requires_a_model(self):
        with pytest.raises(RuntimeError, match="opencode model must be selected"):
            opencode.AGENT.configure({"workspace": WS}, ConfigureRequest())

    def test_configure_returns_state_not_the_write_tuple(self, monkeypatch):
        written = {"workspace": WS, "marker": True}
        calls: list[tuple] = []

        def fake_write(state, model, token=None):
            calls.append((state, model))
            return written, "tok"

        monkeypatch.setattr(opencode, "write_tool_config", fake_write)

        state = {"workspace": WS}
        result = opencode.AGENT.configure(state, ConfigureRequest(model="claude-sonnet"))

        assert result is written
        assert calls == [(state, "claude-sonnet")]

    def test_configure_writes_the_overlay(self, oc_paths):
        config, _ = oc_paths
        state = {
            "workspace": WS,
            "base_urls": {"opencode": _base_urls()},
            "opencode_models": {"anthropic": ["claude-sonnet"]},
        }
        with (
            patch("ucode.agents.opencode.get_databricks_token", return_value="tok"),
            patch("ucode.agents.opencode.save_state"),
        ):
            result = opencode.AGENT.configure(state, ConfigureRequest(model="claude-sonnet"))

        assert isinstance(result, dict)
        assert json.loads(config.read_text())["model"] == "databricks-anthropic/claude-sonnet"

    def test_launch_delegates_to_the_module_launch(self, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(
            opencode, "launch", lambda state, args, *, options: seen.append((state, args, options))
        )
        options = LaunchOptions()

        opencode.AGENT.launch({"workspace": WS}, ["run"], options=options)

        assert seen == [({"workspace": WS}, ["run"], options)]

    def test_revert_reports_restored_or_unchanged(self, monkeypatch, oc_paths):
        config, backup = oc_paths
        calls: list[tuple] = []

        def fake_restore(path, backup_path, managed):
            calls.append((path, backup_path, managed))
            return True

        monkeypatch.setattr(opencode, "restore_file", fake_restore)

        rows = opencode.AGENT.revert({"managed_configs": {"opencode": True}})

        assert rows == [("OpenCode config", "restored")]
        assert calls == [(config, backup, True)]

        monkeypatch.setattr(opencode, "restore_file", lambda *_a: False)
        assert opencode.AGENT.revert({}) == [("OpenCode config", "unchanged")]


def _mcp() -> McpClient:
    client = opencode.AGENT.mcp
    assert client is not None
    return client


class TestOpenCodeMcpClient:
    def test_is_the_agents_mcp_client(self):
        client = _mcp()
        assert isinstance(client, McpClient)
        assert (client.display, client.binary, client.oauth_client_id) == (
            "OpenCode",
            "opencode",
            None,
        )

    def test_add_records_the_proxy_entry_and_reports_a_replaced_entry(self, oc_paths):
        config, _ = oc_paths
        assert _mcp().add("github", _server()) == []
        assert _mcp().add("github", _server()) == [MCP_USER_SCOPE]
        assert json.loads(config.read_text())["mcp"]["github"] == {
            "type": "local",
            "command": ["ug", "mcp-proxy", "--url", "u"],
            "enabled": True,
        }

    def test_add_ignores_an_oauth_client_and_uses_the_proxy(self, oc_paths):
        config, _ = oc_paths
        _mcp().add("github", _server(oauth_client="some-app"))
        assert json.loads(config.read_text())["mcp"]["github"]["type"] == "local"

    def test_remove_reports_the_scope_only_when_it_was_there(self, oc_paths):
        config, _ = oc_paths
        config.write_text(json.dumps({"mcp": {"github": {}, "keep": {}}}))

        assert _mcp().remove("github") == [MCP_USER_SCOPE]
        assert _mcp().remove("github") == []
        assert list(json.loads(config.read_text())["mcp"]) == ["keep"]

    def test_apply_writes_adds_and_removes_in_one_pass(self, oc_paths):
        config, _ = oc_paths
        config.write_text(json.dumps({"provider": {"p": 1}, "mcp": {"gone": {}, "mine": {"a": 1}}}))

        removed = _mcp().apply({"svc": _server()}, {"gone", "absent"})

        doc = json.loads(config.read_text())
        assert removed == {"gone"}
        assert doc["provider"] == {"p": 1}
        assert doc["mcp"]["mine"] == {"a": 1}
        assert doc["mcp"]["svc"]["command"] == ["ug", "mcp-proxy", "--url", "u"]

    def test_apply_with_no_changes_removes_nothing(self, oc_paths):
        assert _mcp().apply({}, set()) == set()

    def test_live_status_parses_the_health_listing(self, monkeypatch):
        from ucode import mcp

        seen: list[tuple] = []

        def fake_listing(argv, *, env=None):
            seen.append((argv, env))
            return "github: ug mcp-proxy - \u2713 Connected\n"

        monkeypatch.setattr(mcp, "_read_mcp_listing", fake_listing)

        assert _mcp().live_status() == {"github": mcp.LIVE_CONNECTED}
        assert seen == [(["opencode", "mcp", "list"], None)]

    def test_live_status_is_empty_when_the_listing_cannot_be_read_or_has_no_servers(
        self, monkeypatch
    ):
        from ucode import mcp

        monkeypatch.setattr(mcp, "_read_mcp_listing", lambda *a, **k: None)
        assert _mcp().live_status() == {}
        monkeypatch.setattr(mcp, "_read_mcp_listing", lambda *a, **k: "No MCP servers configured.")
        assert _mcp().live_status() == {}
