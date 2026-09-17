"""Tests for agents/claude.py."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from ucode import managed_files
from ucode.agents import LaunchOptions, claude
from ucode.smart_routing import claude_routing, v2
from ucode.state import MANAGED_OVERLAY_KEY

WS = "https://example.databricks.com"
# A connection MCP proxy argv, used by the Claude MCP-registration helper tests.
# The leading element is the resolved `ug` binary path, so tests assert the tail.
GH_URL = f"{WS}/api/2.0/mcp/external/github"


def _proxy_argv() -> list[str]:
    from ucode.databricks import build_mcp_proxy_argv

    return build_mcp_proxy_argv(GH_URL, WS, "p")


@pytest.fixture(autouse=True)
def _avoid_real_managed_settings(monkeypatch):
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: None)


class TestClaudeSpec:
    def test_binary(self):
        assert claude.SPEC["binary"] == "claude"

    def test_package(self):
        assert claude.SPEC["package"] == "@anthropic-ai/claude-code"

    def test_display(self):
        assert claude.SPEC["display"] == "Claude Code"


class TestMinimumVersion:
    @pytest.mark.parametrize("version", ["2.1.248", "2.1.250", "3.0.0"])
    def test_supported_version(self, monkeypatch, version):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: version)

        assert claude.minimum_version_error() is None

    def test_older_version_requires_update(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.1.247")

        assert claude.minimum_version_error() == (
            "Smart routing requires Claude Code 2.1.248 or newer. "
            "Your current version is Claude Code 2.1.247."
        )

    def test_older_version_requires_update_for_model_discovery(self, monkeypatch):
        monkeypatch.setenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, "1")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.1.247")

        expected = (
            "Model discovery requires Claude Code 2.1.248 or newer. "
            "Your current version is Claude Code 2.1.247."
        )
        assert claude.minimum_version_error() == expected

    def test_smart_routing_message_wins_when_both_features_are_enabled(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, "1")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.1.247")

        assert claude.minimum_version_error().startswith("Smart routing requires")

    def test_unknown_version_does_not_block(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "unknown")

        assert claude.minimum_version_error() is None

    def test_older_version_is_not_validated_without_discovery_features(self, monkeypatch):
        monkeypatch.delenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
        monkeypatch.delenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, raising=False)
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.1.247")

        assert claude.minimum_version_error() is None


class TestRenderOverlay:
    def test_long_context_suffix_supports_major_only_claude_versions(self):
        assert claude._maybe_add_1m_suffix("system.ai.claude-sonnet-5") == (
            "system.ai.claude-sonnet-5[1m]"
        )

    def test_does_not_set_anthropic_model_env(self):
        # We deliberately don't pin ANTHROPIC_MODEL: when set, Claude Code's
        # /model picker surfaces a duplicate catalog row on top of the family
        # alias from ANTHROPIC_DEFAULT_OPUS_MODEL. Default falls back to the
        # active family alias instead.
        overlay, _ = claude.render_overlay(
            WS, "databricks-claude-opus-4-7", claude_models={"opus": "databricks-claude-opus-4-7"}
        )
        assert "ANTHROPIC_MODEL" not in overlay["env"]

    def test_adds_1m_suffix_for_opus_4_6_and_later(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"opus": "databricks-claude-opus-4-7"}
        )
        assert overlay["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-7[1m]"

    def test_adds_1m_suffix_for_sonnet_4_6_and_later(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"sonnet": "databricks-claude-sonnet-4-7"}
        )
        assert (
            overlay["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-7[1m]"
        )

    def test_does_not_add_1m_suffix_for_haiku(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"haiku": "databricks-claude-haiku-4-6"}
        )
        assert overlay["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "databricks-claude-haiku-4-6"

    def test_does_not_duplicate_1m_suffix(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"opus": "databricks-claude-opus-4-7[1m]"}
        )
        assert overlay["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-7[1m]"

    def test_adds_1m_suffix_for_model_services_name(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"opus": "system.ai.claude-opus-4-8"}
        )
        assert overlay["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"

    def test_no_1m_suffix_for_model_services_haiku(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={"haiku": "system.ai.claude-haiku-4-6"}
        )
        assert overlay["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "system.ai.claude-haiku-4-6"

    def test_custom_model_does_not_persist_model_selection(self):
        # Explicit model selection is launch-scoped and must not be written to settings.
        overlay, _ = claude.render_overlay(
            WS,
            "s4",
            claude_models={"opus": "system.ai.claude-opus-4-8", "sonnet": "system.ai.sonnet"},
            custom_model="main.aarushi.claude-opus-5",
        )
        env = overlay["env"]
        assert "ANTHROPIC_MODEL" not in env
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "system.ai.sonnet"
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
        # No [1m] suffix is appended to the custom id — it's passed through verbatim.
        assert "main.aarushi.claude-opus-5" not in env.values()

    def test_custom_model_does_not_persist_fable_selection(self):
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models={}, custom_model="system.ai.claude-fable-5"
        )
        assert "ANTHROPIC_MODEL" not in overlay["env"]
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in overlay["env"]

    def test_sets_anthropic_base_url(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert overlay["env"]["ANTHROPIC_BASE_URL"] == f"{WS}/ai-gateway/anthropic"

    def test_sets_custom_headers(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "x-databricks-use-coding-agent-mode" in overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    def test_does_not_disable_experimental_betas(self):
        # Would suppress the beta header 1h prompt caching needs.
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in overlay["env"]

    def test_enables_prompt_caching_1h(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert overlay["env"]["ENABLE_PROMPT_CACHING_1H"] == "1"

    def test_enables_tool_search(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert overlay["env"]["ENABLE_TOOL_SEARCH"] == "true"

    def test_enables_use_gateway(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert overlay["env"]["CLAUDE_CODE_USE_GATEWAY"] == "1"

    @pytest.mark.parametrize("env_value", [None, "", "0", "true", "yes"])
    def test_gateway_model_discovery_disabled_unless_opted_in(self, monkeypatch, env_value):
        if env_value is not None:
            monkeypatch.setenv("ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY", env_value)
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in overlay["env"]

    def test_does_not_persist_gateway_model_discovery(self, monkeypatch):
        monkeypatch.setenv("ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY", "1")
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in overlay["env"]

    def test_smart_routing_does_not_persist_gateway_model_discovery(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.delenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, raising=False)
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in overlay["env"]

    def test_gateway_model_discovery_not_persisted_under_provider(self, monkeypatch):
        monkeypatch.setenv("ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY", "1")
        overlay, _ = claude.render_overlay(WS, "s4", provider="main.x.claude-svc")
        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in overlay["env"]

    def test_gateway_model_discovery_setting_detects_stale_opt_in(self, monkeypatch):
        monkeypatch.delenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, raising=False)
        monkeypatch.setattr(
            claude,
            "read_json_safe",
            lambda path: {"env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}},
        )

        assert claude.gateway_model_discovery_setting_is_absent() is False

    def test_sets_api_key_helper(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/my tools/{command}")
        overlay, _ = claude.render_overlay(WS, "s4")
        assert shlex.split(overlay["apiKeyHelper"]) == ["/my tools/ug", "auth-token", "--host", WS]

    def test_sets_custom_oauth_api_key_helper(self, monkeypatch):
        from ucode import custom_oauth

        monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/opt/ug")
        monkeypatch.setattr(custom_oauth.platform, "system", lambda: "Linux")
        overlay, _ = claude.render_overlay(
            WS,
            "s4",
            custom_oauth={
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
            },
        )
        assert shlex.split(overlay["apiKeyHelper"]) == [
            "/opt/ug",
            "auth-token",
            "--host",
            WS,
            "--client-id",
            "custom-client",
            "--redirect-url",
            "http://localhost:8020/callback",
            "--scopes",
            "offline_access,model-serving",
        ]

    def test_saved_custom_oauth_profile_uses_minimal_api_key_helper(self, monkeypatch):
        from ucode import custom_oauth

        monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/opt/ug")
        monkeypatch.setattr(custom_oauth.platform, "system", lambda: "Linux")
        overlay, _ = claude.render_overlay(
            WS,
            "s4",
            custom_oauth={
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
                "profile": "custom-profile",
            },
        )
        assert shlex.split(overlay["apiKeyHelper"]) == [
            "/opt/ug",
            "auth-token",
            "--host",
            WS,
            "--profile",
            "custom-profile",
        ]

    def test_relayed_omits_api_key_helper(self):
        # Claude Code's own subscription OAuth must own Authorization; an
        # apiKeyHelper would outrank it.
        overlay, _ = claude.render_overlay(
            WS,
            None,
            provider="c.s.mps",
            relayed=True,
            relayed_base_url="http://127.0.0.1:9",
        )
        assert "apiKeyHelper" not in overlay

    def test_relayed_points_base_url_at_proxy(self):
        overlay, _ = claude.render_overlay(
            WS,
            None,
            provider="c.s.mps",
            relayed=True,
            relayed_base_url="http://127.0.0.1:9",
        )
        assert overlay["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"

    def test_relayed_sends_mps_header_but_not_swap_token(self):
        # The MPS header selects the service; the swap token is injected by the
        # proxy, never written into settings.
        overlay, _ = claude.render_overlay(
            WS,
            None,
            provider="c.s.mps",
            relayed=True,
            relayed_base_url="http://127.0.0.1:9",
        )
        headers = overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        assert "Databricks-Model-Provider-Service: c.s.mps" in headers
        assert "X-Databricks-AI-Gateway-Token" not in headers

    def test_model_overrides_when_all_provided(self):
        models = {
            "sonnet": "databricks-claude-sonnet-4-6",
            "opus": "databricks-claude-opus-4-7",
            "haiku": "databricks-claude-haiku-4-6",
        }
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        env = overlay["env"]
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-6[1m]"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-7[1m]"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "databricks-claude-haiku-4-6"

    def test_model_overrides_partial(self):
        models = {"sonnet": "s4"}
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        env = overlay["env"]
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" in env
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env

    def test_model_overrides_not_set_when_no_models(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        env = overlay["env"]
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env

    def test_fable_pinned_by_default_when_discovered(self):
        models = {"fable": "databricks-claude-fable-5", "opus": "databricks-claude-opus-4-8"}
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        env = overlay["env"]
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "databricks-claude-fable-5"
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "databricks-claude-opus-4-8[1m]"

    def test_discovered_fable_uses_unsuffixed_model_id(self):
        models = {"fable": "system.ai.claude-fable-5"}
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        env = overlay["env"]
        # Fable 5 is 1M-context by default, so no `[1m]` suffix is appended.
        assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "system.ai.claude-fable-5"

    def test_fable_not_pinned_when_not_discovered(self):
        models = {"opus": "databricks-claude-opus-4-8"}
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in overlay["env"]

    def test_fable_not_pinned_under_provider(self):
        # A Model Provider Service routes by header and pins no Databricks model.
        models = {"fable": "databricks-claude-fable-5"}
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models=models, provider="main.x.claude-svc"
        )
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in overlay["env"]

    def test_provider_adds_routing_header(self):
        overlay, _ = claude.render_overlay(WS, "s4", provider="main.aarushi.aarushi-claude")
        assert (
            "Databricks-Model-Provider-Service: main.aarushi.aarushi-claude"
            in overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        )

    def test_provider_skips_model_pinning(self):
        models = {
            "opus": "databricks-claude-opus-4-7",
            "sonnet": "databricks-claude-sonnet-4-6",
            "haiku": "databricks-claude-haiku-4-6",
        }
        overlay, _ = claude.render_overlay(
            WS, "s4", claude_models=models, provider="main.aarushi.aarushi-claude"
        )
        env = overlay["env"]
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env

    def test_no_provider_header_without_flag(self):
        overlay, _ = claude.render_overlay(WS, "s4")
        assert "Databricks-Model-Provider-Service" not in overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    def test_parent_adds_discovery_header(self):
        overlay, _ = claude.render_overlay(WS, "s4", parent_schema="main.default")
        assert (
            "Databricks-Model-Service-Parent-Schema: main.default"
            in overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        )

    def test_bedrock_provider_pins_model_ids(self):
        provider_models = {
            "opus": "global.anthropic.claude-opus-4-8",
            "sonnet": "us.anthropic.claude-sonnet-4-6",
            "haiku": "anthropic.claude-haiku-4-5",
        }
        overlay, _ = claude.render_overlay(
            WS,
            None,
            provider="main.bob.bedrock-svc",
            provider_models=provider_models,
        )
        env = overlay["env"]
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "global.anthropic.claude-opus-4-8"
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "us.anthropic.claude-sonnet-4-6"
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "anthropic.claude-haiku-4-5"
        # Bedrock ids are pinned verbatim — no `[1m]` suffix mangling.
        assert "[1m]" not in env["ANTHROPIC_DEFAULT_OPUS_MODEL"]
        assert (
            "Databricks-Model-Provider-Service: main.bob.bedrock-svc"
            in env["ANTHROPIC_CUSTOM_HEADERS"]
        )

    def test_non_relayed_provider_pins_tier_via_anthropic_model(self):
        # A non-relayed api-key Anthropic MPS launched on a specific tier: the tier
        # rides ANTHROPIC_MODEL (route_root_model), the routing header selects the
        # service, and the gateway apiKeyHelper is still written (unlike relayed).
        overlay, _ = claude.render_overlay(
            WS,
            None,
            provider="main.mcao.anthropic-mps",
            route_root_model="claude-haiku-4-5",
        )
        env = overlay["env"]
        assert env["ANTHROPIC_MODEL"] == "claude-haiku-4-5"
        assert (
            "Databricks-Model-Provider-Service: main.mcao.anthropic-mps"
            in (env["ANTHROPIC_CUSTOM_HEADERS"])
        )
        assert "apiKeyHelper" in overlay

    def test_picker_labels_show_raw_routable_id(self):
        # We deliberately don't set the `_NAME` companion env vars. Showing the
        # raw `system.ai.…` / `databricks-…` id in the picker label tells users
        # exactly which gateway-routable model is behind each shortcut, which is
        # more useful than a friendly catalog label for Databricks routing.
        models = {
            "opus": "system.ai.claude-opus-4-8",
            "sonnet": "databricks-claude-sonnet-4-6",
            "haiku": "system.ai.claude-haiku-4-5",
        }
        overlay, _ = claude.render_overlay(WS, "s4", claude_models=models)
        env = overlay["env"]
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME" not in env
        assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "databricks-claude-sonnet-4-6[1m]"
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in env
        assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "system.ai.claude-haiku-4-5"
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME" not in env

    def test_managed_keys_include_api_key_helper(self):
        _, keys = claude.render_overlay(WS, "s4")
        assert ["apiKeyHelper"] in keys

    def test_managed_keys_include_env_entries(self):
        _, keys = claude.render_overlay(WS, "s4")
        env_keys = [k for k in keys if len(k) == 2 and k[0] == "env"]
        assert len(env_keys) > 0

    def test_static_models_populates_picker(self):
        # Static models are written into the picker allow-list.
        static = ["system.ai.claude-opus-4-8", "system.ai.claude-sonnet-4-6"]
        overlay, keys = claude.render_overlay(WS, "s4", static_models=static)
        assert overlay["availableModels"] == static
        assert overlay["enforceAvailableModels"] is True
        assert overlay["modelPicker"]["replaceBuiltInOptions"] is True
        assert len(overlay["modelPicker"]["options"]) == 2
        assert overlay["modelPicker"]["options"][0]["model"] == "system.ai.claude-opus-4-8"
        assert overlay["modelPicker"]["options"][0]["label"] == "Claude Opus 4.8"

    def test_static_models_keys_tracked(self):
        # The picker keys are added to managed_keys so they're tracked in the managed file.
        static = ["system.ai.claude-opus-4-8"]
        _, keys = claude.render_overlay(WS, "s4", static_models=static)
        assert ["availableModels"] in keys
        assert ["enforceAvailableModels"] in keys
        assert ["modelPicker"] in keys

    def test_static_models_skipped_when_provider_set(self):
        # When routing through an MPS provider, static models are ignored.
        static = ["system.ai.claude-opus-4-8"]
        overlay, _ = claude.render_overlay(WS, "s4", provider="main.x.mps", static_models=static)
        assert "availableModels" not in overlay
        assert "modelPicker" not in overlay

    def test_static_models_skipped_when_relayed(self):
        # When using relayed inference, static models are ignored.
        static = ["system.ai.claude-opus-4-8"]
        overlay, _ = claude.render_overlay(
            WS, "s4", relayed=True, relayed_base_url="http://localhost:8000", static_models=static
        )
        assert "availableModels" not in overlay
        assert "modelPicker" not in overlay

    def test_static_models_label_is_prettified(self):
        # Picker labels drop the ``system.ai.`` prefix and are title-cased with a dotted version.
        static = [
            "system.ai.claude-opus-4-8",
            "system.ai.glm-5-3",
            "system.ai.kimi-k3",
            "databricks-custom-model",
        ]
        overlay, _ = claude.render_overlay(WS, "s4", static_models=static)
        labels = [opt["label"] for opt in overlay["modelPicker"]["options"]]
        assert labels == ["Claude Opus 4.8", "GLM 5.3", "Kimi K3", "Databricks Custom Model"]


class TestRenderOverlayOtelTracing:
    def test_otel_tracing_off_by_default(self):
        overlay, _ = claude.render_overlay(WS, "s4", claude_models={"opus": "system.ai.x"})
        assert "otelHeadersHelper" not in overlay
        assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in overlay["env"]
        assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in overlay["env"]

    def test_otel_tracing_writes_env_and_refreshing_headers_helper(self):
        overlay, _ = claude.render_overlay(WS, "s4", otel_tracing=True)
        env = overlay["env"]
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
        assert env["OTEL_TRACES_EXPORTER"] == "otlp"
        assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
        assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"{WS}/ai-gateway/otel/v1/traces"
        assert env["CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS"] == "900000"
        assert env["CLAUDE_CODE_PROPAGATE_TRACEPARENT"] == "1"
        assert "otel-headers" in overlay["otelHeadersHelper"]
        assert "OTEL_EXPORTER_OTLP_TRACES_HEADERS" not in env

    def test_otel_tracing_keys_are_managed(self):
        _, keys = claude.render_overlay(WS, "s4", otel_tracing=True)
        assert ["otelHeadersHelper"] in keys
        assert ["env", "CLAUDE_CODE_ENABLE_TELEMETRY"] in keys
        assert ["env", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] in keys


class TestRenderOverlayUserAgent:
    def _ua(self, monkeypatch) -> str:
        monkeypatch.setattr(claude, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(claude, "agent_version", lambda binary: "2.1.136")
        overlay, _ = claude.render_overlay(WS, "s4")
        return overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]

    def test_user_agent_present(self, monkeypatch):
        assert "User-Agent: ucode/0.1.0 claude/2.1.136" in self._ua(monkeypatch)

    def test_existing_databricks_header_preserved(self, monkeypatch):
        assert "x-databricks-use-coding-agent-mode: true" in self._ua(monkeypatch)

    def test_headers_newline_delimited(self, monkeypatch):
        assert "\n" in self._ua(monkeypatch)


class TestMergeAnthropicCustomHeaders:
    def test_removes_stale_parent_header(self):
        existing = "X-User: keep\nDatabricks-Model-Service-Parent-Schema: main.default"
        managed = "x-databricks-use-coding-agent-mode: true"

        merged = claude._merge_anthropic_custom_headers(existing, managed)

        assert "X-User: keep" in merged
        assert "Databricks-Model-Service-Parent-Schema" not in merged

    def test_merges_existing_settings_with_ucode_managed_headers(self):
        headers_from_existing_settings = "\n".join(
            [
                "X-User-Header: keep-me",
                "user-agent: custom-agent",
            ]
        )
        headers_managed_by_ucode = "\n".join(
            [
                "x-databricks-use-coding-agent-mode: true",
                "User-Agent: ucode/1.0 claude/2.0",
            ]
        )

        merged_headers = claude._merge_anthropic_custom_headers(
            headers_from_existing_settings, headers_managed_by_ucode
        )

        assert merged_headers.splitlines() == [
            "X-User-Header: keep-me",  # Preserved from existing settings.
            "User-Agent: ucode/1.0 claude/2.0",  # From ucode; overwrites existing.
            "x-databricks-use-coding-agent-mode: true",  # Newly added by ucode.
        ]

    def test_preserves_existing_header_order(self):
        headers_from_existing_settings = "\n".join(
            [
                "x-databricks-use-coding-agent-mode: true",
                "User-Agent: ucode/0.1.0+41.gd09c080 claude/2.1.258",
                "meep: lala",
            ]
        )
        headers_managed_by_ucode = "\n".join(
            [
                "x-databricks-use-coding-agent-mode: true",
                "User-Agent: ucode/1.0 claude/2.0",
            ]
        )

        merged_headers = claude._merge_anthropic_custom_headers(
            headers_from_existing_settings, headers_managed_by_ucode
        )

        assert merged_headers.splitlines() == [
            "x-databricks-use-coding-agent-mode: true",  # From ucode; overwrites existing.
            "User-Agent: ucode/1.0 claude/2.0",  # From ucode; overwrites existing.
            "meep: lala",  # Preserved from existing settings in its original position.
        ]


class TestRenderOverlayWebSearchDisable:
    def test_settings_overlay_never_includes_mcp_servers(self):
        # MCP servers belong in ~/.claude.json, not settings.json.
        overlay, _ = claude.render_overlay(WS, "s4", disable_web_search=True)
        assert "mcpServers" not in overlay

    def test_disables_builtin_websearch_when_requested(self):
        # A bare `permissions.deny` entry removes the built-in WebSearch tool
        # from Claude's context (Claude Code has no `disabledTools` setting).
        overlay, _ = claude.render_overlay(WS, "s4", disable_web_search=True)
        assert overlay["permissions"] == {"deny": ["WebSearch"]}

    def test_no_disable_when_not_requested(self):
        overlay, _ = claude.render_overlay(WS, "s4", disable_web_search=False)
        assert "permissions" not in overlay

    def test_managed_keys_include_disabled_tools_when_set(self):
        _, keys = claude.render_overlay(WS, "s4", disable_web_search=True)
        assert ["permissions", "deny"] in keys

    def test_managed_keys_omit_disabled_tools_when_not_set(self):
        _, keys = claude.render_overlay(WS, "s4", disable_web_search=False)
        assert ["permissions", "deny"] not in keys


class TestWebSearchMcpEntry:
    def test_entry_shape(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/tools/{command}")
        entry = claude._web_search_mcp_entry(WS, "databricks-gpt-5")
        assert entry["type"] == "stdio"
        assert entry["args"] == ["mcp", "web-search"]
        assert entry["env"]["DATABRICKS_HOST"] == WS
        assert entry["env"]["UCODE_WEB_SEARCH_MODEL"] == "databricks-gpt-5"
        assert entry["command"] == "/tools/ug"


class TestResolveWebSearchModel:
    def test_uses_explicit_override(self):
        assert claude._resolve_web_search_model({"web_search_model": "explicit"}) == "explicit"

    def test_falls_back_to_first_codex_model(self):
        state = {"codex_models": ["m1", "m2"]}
        assert claude._resolve_web_search_model(state) == "m1"

    def test_returns_none_when_no_codex_models(self):
        assert claude._resolve_web_search_model({}) is None
        assert claude._resolve_web_search_model({"codex_models": []}) is None

    def test_override_wins_over_codex_models(self):
        state = {"web_search_model": "winner", "codex_models": ["loser"]}
        assert claude._resolve_web_search_model(state) == "winner"


class TestClaudeDefaultModel:
    def test_prefers_opus(self):
        state = {"claude_models": {"fable": "f5", "sonnet": "s4", "opus": "o4", "haiku": "h4"}}
        assert claude.default_model(state) == "o4"

    def test_falls_back_to_sonnet(self):
        state = {"claude_models": {"sonnet": "s4", "haiku": "h4"}}
        assert claude.default_model(state) == "s4"

    def test_falls_back_to_haiku(self):
        state = {"claude_models": {"fable": "f5", "haiku": "h4"}}
        assert claude.default_model(state) == "h4"

    def test_fable_only_workspace_has_a_default(self):
        assert claude.default_model({"claude_models": {"fable": "f5"}}) == "f5"

    def test_returns_none_when_no_models(self):
        assert claude.default_model({}) is None
        assert claude.default_model({"claude_models": {}}) is None


class TestClaudeValidateCmd:
    def test_starts_with_binary(self):
        cmd = claude.validate_cmd("claude")
        assert cmd[0] == "claude"

    def test_has_p_flag(self):
        cmd = claude.validate_cmd("claude")
        assert "-p" in cmd

    def test_uses_ucode_settings_file(self):
        cmd = claude.validate_cmd("claude")
        assert cmd[:3] == ["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH)]

    def test_has_max_turns(self):
        cmd = claude.validate_cmd("claude")
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "1"


class TestWriteToolConfigMcpRegistration:
    def _common_patches(self, monkeypatch, calls):
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "read_json_safe", lambda path: {})
        monkeypatch.setattr(claude, "write_json_file", lambda path, payload: None)
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(
            claude,
            "_register_web_search_mcp",
            lambda ws, model, profile=None: calls.append(("register", ws, model)),
        )

    def test_registers_mcp_when_codex_model_available(self, monkeypatch):
        calls: list = []
        self._common_patches(monkeypatch, calls)
        state = {"workspace": WS, "codex_models": ["databricks-gpt-5"]}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        assert calls == [("register", WS, "databricks-gpt-5")]

    def test_skips_registration_without_codex_model(self, monkeypatch):
        calls: list = []
        self._common_patches(monkeypatch, calls)
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        assert calls == []

    def test_explicit_override_used_over_codex_models(self, monkeypatch):
        calls: list = []
        self._common_patches(monkeypatch, calls)
        state = {
            "workspace": WS,
            "web_search_model": "explicit-model",
            "codex_models": ["other-model"],
        }
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        assert calls == [("register", WS, "explicit-model")]


class TestWriteToolConfigStripsRemovedEnvKeys:
    """Stale keys ucode no longer writes are dropped from the merged settings."""

    def _patch(self, monkeypatch, existing, written):
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "read_json_safe", lambda path: existing)
        monkeypatch.setattr(
            claude, "write_json_file", lambda path, payload: written.append(payload)
        )
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)

    def test_strips_stale_disable_experimental_betas(self, monkeypatch):
        existing = {"env": {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"}}
        written: list = []
        self._patch(monkeypatch, existing, written)
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in written[0]["env"]
        assert written[0]["env"]["ENABLE_PROMPT_CACHING_1H"] == "1"
        assert written[0]["env"]["ENABLE_TOOL_SEARCH"] == "true"
        assert written[0]["env"]["CLAUDE_CODE_USE_GATEWAY"] == "1"

    def test_strips_stale_gateway_model_discovery(self, monkeypatch):
        existing = {"env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}}
        written: list = []
        self._patch(monkeypatch, existing, written)
        state = {"workspace": WS, "codex_models": []}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in written[0]["env"]

    def test_writes_otel_tracing_when_enabled(self, monkeypatch):
        written: list = []
        self._patch(monkeypatch, {}, written)
        state = {"workspace": WS, "codex_models": [], "claude_otel_tracing": True}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        env = written[0]["env"]
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
        assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"{WS}/ai-gateway/otel/v1/traces"
        assert "otel-headers" in written[0]["otelHeadersHelper"]

    def test_strips_stale_otel_tracing_when_disabled(self, monkeypatch):
        existing = {
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "OTEL_TRACES_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{WS}/ai-gateway/otel/v1/traces",
            },
            "otelHeadersHelper": f"ug otel-headers --host {WS}",
        }
        written: list = []
        self._patch(monkeypatch, existing, written)

        claude.write_tool_config(
            {"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4"
        )

        assert "otelHeadersHelper" not in written[0]
        for key in claude.CLAUDE_OTEL_TRACE_ENV_KEYS:
            assert key not in written[0]["env"]


FAKE_MANAGED_PATH = Path("/tmp/ucode-test/managed-settings.json")


class TestWriteToolConfigManagedSettings:
    """Every normal configuration also writes Claude Code's OS-managed settings."""

    def _patch(self, monkeypatch, private_writes, managed_writes, existing_by_path=None):
        existing_by_path = existing_by_path or {}
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        # Deep-copy the seeded existing content so the compose step can't mutate the fixture.
        monkeypatch.setattr(
            claude,
            "read_json_safe",
            lambda path: json.loads(json.dumps(existing_by_path.get(str(path), {}))),
        )
        monkeypatch.setattr(
            claude,
            "write_json_file",
            lambda path, payload: private_writes.append((str(path), payload)),
        )
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        # By default ucode has no baseline/last-applied snapshot, so an admin's managed-file picker
        # is kept (nothing to revert against).
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots(None, None),
        )
        # Deterministic managed path, and a mocked sudo writer so NO real sudo/`/etc` write happens.
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: FAKE_MANAGED_PATH)
        monkeypatch.setattr(
            claude,
            "read_managed_file",
            lambda path: (
                json.dumps(existing_by_path[str(path)]) if str(path) in existing_by_path else None
            ),
        )
        monkeypatch.setattr(claude, "mark_managed_file_verified", lambda *a, **kw: None)

        def fake_write_managed(path, text, **kwargs):
            managed_writes.append((str(path), text))
            return "written"

        monkeypatch.setattr(claude, "reconcile_managed_file", fake_write_managed)

    def _write_managed_model_defaults(
        self,
        monkeypatch,
        *,
        coding_agent_config_defaults: dict[str, str],
        managed_settings_defaults: dict[str, str],
        ucode_defaults: dict[str, str],
    ) -> dict[str, str]:
        private_writes: list = []
        managed_writes: list = []
        managed_settings_env = {
            claude.CLAUDE_DEFAULT_MODEL_ENV_KEYS[family]: model
            for family, model in managed_settings_defaults.items()
        }
        self._patch(
            monkeypatch,
            private_writes,
            managed_writes,
            {str(FAKE_MANAGED_PATH): {"env": managed_settings_env}},
        )
        resolved_defaults = coding_agent_config_defaults or ucode_defaults
        state = {
            "workspace": WS,
            "codex_models": [],
            "claude_models": resolved_defaults,
        }
        if coding_agent_config_defaults:
            state[MANAGED_OVERLAY_KEY] = {"claude_models": ucode_defaults}

        claude.write_tool_config(
            state,
            next(iter(resolved_defaults.values()), "test-model"),
            coding_agent_config_defaults=coding_agent_config_defaults,
        )

        _, text = managed_writes[0]
        written_env = json.loads(text)["env"]
        return {
            family: written_env[key]
            for family, key in claude.CLAUDE_DEFAULT_MODEL_ENV_KEYS.items()
            if key in written_env
        }

    def test_writes_managed_file_by_default(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        # Private file still written; managed file written too.
        assert str(claude.CLAUDE_SETTINGS_PATH) in [p for p, _ in private_writes]
        assert [p for p, _ in managed_writes] == [str(FAKE_MANAGED_PATH)]
        assert "modelPicker" not in private_writes[0][1]
        assert "modelPicker" not in json.loads(managed_writes[0][1])

    def test_managed_file_preserves_other_keys(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # An IT-authored key already in the managed file must survive the merge.
        existing = {str(FAKE_MANAGED_PATH): {"env": {"MY_OWN": "keep"}}}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        _, text = managed_writes[0]
        written = json.loads(text)
        assert written["env"]["MY_OWN"] == "keep"
        assert written["env"]["ANTHROPIC_BASE_URL"]
        assert written["apiKeyHelper"]

    def test_managed_file_updates_gateway_settings_without_changing_model_picker(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        picker = {
            "replaceBuiltInOptions": True,
            "options": [
                {"model": "system.ai.claude-opus-4-8"},
                {"model": "system.ai.glm-5-2"},
            ],
        }
        existing = {
            str(FAKE_MANAGED_PATH): {
                "modelPicker": picker,
                "env": {
                    "ANTHROPIC_BASE_URL": "https://old-workspace.databricks.com/ai-gateway/anthropic"
                },
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        state = {"workspace": WS, "codex_models": []}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        written = json.loads(managed_writes[0][1])
        assert written["modelPicker"] == picker
        assert written["env"]["ANTHROPIC_BASE_URL"] == f"{WS}/ai-gateway/anthropic"

    def test_managed_file_strips_stale_gateway_model_discovery(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        stale = {"env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"}}
        existing = {
            str(claude.CLAUDE_SETTINGS_PATH): stale,
            str(FAKE_MANAGED_PATH): stale,
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)

        state = {"workspace": WS, "codex_models": []}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        assert "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY" not in private_writes[0][1]["env"]
        assert (
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
            not in json.loads(managed_writes[0][1])["env"]
        )

    def test_managed_file_merges_anthropic_custom_headers(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing_managed_settings = {
            str(FAKE_MANAGED_PATH): {
                "env": {"ANTHROPIC_CUSTOM_HEADERS": "X-Enterprise-Header: retain\nUser-Agent: old"}
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing_managed_settings)
        monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
        monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
        state = {"workspace": WS, "codex_models": []}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        _, text = managed_writes[0]
        merged_headers = json.loads(text)["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        assert merged_headers.splitlines() == [
            "X-Enterprise-Header: retain",  # Preserved from existing managed settings.
            "User-Agent: ucode/1.0 claude/2.0",  # From ucode; overwrites existing.
            "x-databricks-use-coding-agent-mode: true",  # Newly added by ucode.
        ]

    def test_managed_file_applies_model_default_precedence(self, monkeypatch):
        managed_defaults = self._write_managed_model_defaults(
            monkeypatch,
            coding_agent_config_defaults={"opus": "system.ai.claude-opus-4-8"},
            managed_settings_defaults={
                "opus": "system.ai.claude-opus-5",
                "sonnet": "system.ai.claude-sonnet-4-6",
            },
            ucode_defaults={
                "opus": "system.ai.claude-opus-5",
                "sonnet": "system.ai.claude-sonnet-5",
                "haiku": "system.ai.claude-haiku-5",
            },
        )

        assert managed_defaults == {
            "opus": "system.ai.claude-opus-4-8[1m]",  # Coding Agent Config took priority.
            "sonnet": "system.ai.claude-sonnet-4-6",  # Existing managed setting took priority.
            "haiku": "system.ai.claude-haiku-5",  # Ucode default took priority.
        }

    def test_managed_file_omits_workspace_defaults_for_provider(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing = {
            str(FAKE_MANAGED_PATH): {
                "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8"}
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        state = {
            "workspace": WS,
            "claude_models": {
                "opus": "system.ai.claude-opus-4-8",
                "haiku": "system.ai.claude-haiku-4-6",
            },
        }

        claude.write_tool_config(state, None, provider="main.default.anthropic")

        env = json.loads(managed_writes[0][1])["env"]
        assert not set(claude.CLAUDE_DEFAULT_MODEL_ENV_KEYS.values()) & env.keys()

    def test_managed_file_keeps_provider_model_pins(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}

        claude.write_tool_config(
            state,
            None,
            provider="main.default.bedrock",
            provider_models={"opus": "us.anthropic.claude-opus-4-6"},
        )

        env = json.loads(managed_writes[0][1])["env"]
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "us.anthropic.claude-opus-4-6"

    def test_managed_file_prunes_static_picker_when_switching_to_provider(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # A prior static config left an enforced picker in the managed file.
        existing = {
            str(FAKE_MANAGED_PATH): {
                "availableModels": ["system.ai.claude-opus-4-8"],
                "enforceAvailableModels": True,
                "modelPicker": {"replaceBuiltInOptions": True, "options": []},
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        # ucode introduced this picker (absent from the pre-ucode baseline) and the live value still
        # matches its last write, so the whole picker reverts to that empty baseline.
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots(
                {}, existing[str(FAKE_MANAGED_PATH)]
            ),
        )
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}

        # Switching to a Model Provider Service routes by header and enforces no list.
        claude.write_tool_config(state, None, provider="main.default.anthropic")

        written = json.loads(managed_writes[0][1])
        for key in claude.CLAUDE_MANAGED_PICKER_KEYS:
            assert key not in written, written

    def test_managed_file_keeps_admin_picker_added_after_ucode_cleared(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # An administrator authored their own picker after ucode had cleared its earlier one.
        admin_picker = {"replaceBuiltInOptions": True, "options": [{"model": "system.ai.glm-5-2"}]}
        existing = {
            str(FAKE_MANAGED_PATH): {
                "availableModels": ["system.ai.glm-5-2"],
                "enforceAvailableModels": True,
                "modelPicker": admin_picker,
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        # ucode's last write no longer carries a picker (it cleared its own earlier), so the admin's
        # later picker differs from that snapshot and must be kept.
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots({}, {"env": {"MY_OWN": "x"}}),
        )
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}

        claude.write_tool_config(state, None)

        written = json.loads(managed_writes[0][1])
        assert written["availableModels"] == ["system.ai.glm-5-2"], written
        assert written["modelPicker"] == admin_picker, written
        assert written["enforceAvailableModels"] is True, written

    def test_managed_file_keeps_admin_picker_on_repeated_unmanaged_configure(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # An administrator's picker predates ucode. A first unmanaged configure preserved it and, in
        # doing so, wrote it into ucode's own snapshot. This is the SECOND unmanaged configure.
        admin_picker = {"replaceBuiltInOptions": True, "options": [{"model": "system.ai.glm-5-2"}]}
        admin = {
            "availableModels": ["system.ai.glm-5-2"],
            "enforceAvailableModels": True,
            "modelPicker": admin_picker,
        }
        existing = {str(FAKE_MANAGED_PATH): dict(admin)}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        # The picker is in both the pre-ucode baseline and ucode's last write (ucode only preserved
        # it), so matching the snapshot alone does not make it ucode's: reverting to the baseline
        # keeps it. Snapshot equality alone must not delete it.
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots(dict(admin), dict(admin)),
        )
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}

        claude.write_tool_config(state, None)

        written = json.loads(managed_writes[0][1])
        assert written["availableModels"] == ["system.ai.glm-5-2"], written
        assert written["modelPicker"] == admin_picker, written
        assert written["enforceAvailableModels"] is True, written

    def test_managed_file_keeps_admin_edited_picker_as_a_unit(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # After a static config, the administrator edited the model list and picker but kept
        # enforceAvailableModels=True. The whole picker must survive as a unit, flag included.
        admin_picker = {"replaceBuiltInOptions": True, "options": [{"model": "system.ai.glm-5-2"}]}
        existing = {
            str(FAKE_MANAGED_PATH): {
                "availableModels": ["system.ai.glm-5-2"],
                "enforceAvailableModels": True,
                "modelPicker": admin_picker,
            }
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        # ucode last wrote a DIFFERENT static picker (same enforce flag). Because the live picker
        # differs from that write as a unit, none of its fields are touched, so comparing fields
        # independently cannot strip the unchanged enforce flag.
        ucode_last = {
            "availableModels": ["system.ai.claude-opus-4-8"],
            "enforceAvailableModels": True,
            "modelPicker": {"replaceBuiltInOptions": True, "options": []},
        }
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots({}, ucode_last),
        )
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}

        claude.write_tool_config(state, None)

        written = json.loads(managed_writes[0][1])
        assert written["availableModels"] == ["system.ai.glm-5-2"], written
        assert written["modelPicker"] == admin_picker, written
        assert written["enforceAvailableModels"] is True, written

    def test_managed_file_resets_ucode_family_default_outside_the_enforced_list(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # ucode itself wrote a fable default on a previous workspace; the new config lists no fable.
        stale_fable = {"ANTHROPIC_DEFAULT_FABLE_MODEL": "system.ai.claude-fable-5"}
        existing = {str(FAKE_MANAGED_PATH): {"env": dict(stale_fable)}}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots(
                None, {"env": dict(stale_fable)}
            ),
        )
        static = [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-sonnet-4-6",
            "system.ai.claude-haiku-4-5",
        ]
        state = {"workspace": WS, "codex_models": [], "claude_static_models": static}
        claude.write_tool_config(
            state,
            None,
            coding_agent_config_defaults={
                "opus": "system.ai.claude-opus-4-8",
                "sonnet": "system.ai.claude-sonnet-4-6",
                "haiku": "system.ai.claude-haiku-4-5",
            },
        )
        written = json.loads(managed_writes[0][1])["env"]
        assert "ANTHROPIC_DEFAULT_FABLE_MODEL" not in written, written
        assert written["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]", written
        assert written["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "system.ai.claude-haiku-4-5", written

    def test_managed_file_preserves_admin_authored_family_default(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        # An administrator hand-set an opus default ucode never wrote (absent from ucode's last write).
        admin_opus = {"ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-9-admin"}
        existing = {str(FAKE_MANAGED_PATH): {"env": dict(admin_opus)}}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        monkeypatch.setattr(
            claude,
            "managed_file_snapshots",
            lambda tool, parser: managed_files.ManagedFileSnapshots(None, {"env": {}}),
        )
        static = ["system.ai.claude-opus-4-8", "system.ai.claude-sonnet-4-6"]
        state = {"workspace": WS, "codex_models": [], "claude_static_models": static}
        claude.write_tool_config(
            state, None, coding_agent_config_defaults={"sonnet": "system.ai.claude-sonnet-4-6"}
        )
        written = json.loads(managed_writes[0][1])["env"]
        assert written["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-9-admin", written

    def test_managed_file_applies_fable_default_precedence_without_opt_in(self, monkeypatch):
        managed_defaults = self._write_managed_model_defaults(
            monkeypatch,
            coding_agent_config_defaults={"fable": "coding-agent-config-fable"},
            managed_settings_defaults={"fable": "managed-settings-fable"},
            ucode_defaults={"fable": "ucode-fable"},
        )

        assert managed_defaults["fable"] == "coding-agent-config-fable"

    def test_managed_file_preserves_enterprise_permission_denies(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing = {str(FAKE_MANAGED_PATH): {"permissions": {"deny": ["Bash(rm:*)"]}}}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        state = {"workspace": WS, "codex_models": ["databricks-gpt-5"]}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        _, text = managed_writes[0]
        assert json.loads(text)["permissions"]["deny"] == ["Bash(rm:*)", "WebSearch"]

    def test_relayed_skips_managed_write(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        warns: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        monkeypatch.setattr(claude, "print_warning", lambda msg: warns.append(msg))
        monkeypatch.setattr(claude, "relayed_proxy_base_url", lambda state: "http://127.0.0.1:9999")
        monkeypatch.setattr(claude, "_managed_relayed_conflicts", lambda path: [])
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4", relayed=True)
        assert managed_writes == []
        assert warns == []

    def test_relayed_fails_on_conflicting_managed_auth(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing = {str(FAKE_MANAGED_PATH): {"apiKeyHelper": "enterprise-helper"}}
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        monkeypatch.setattr(claude, "relayed_proxy_base_url", lambda state: "http://127.0.0.1:9999")
        state = {"workspace": WS, "codex_models": []}

        with pytest.raises(RuntimeError, match="run `ucode revert`"):
            claude.write_tool_config(state, "databricks-claude-sonnet-4", relayed=True)

        assert managed_writes == []

    def test_relayed_rejects_invalid_managed_json(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        monkeypatch.setattr(claude, "read_managed_file", lambda path: "{")
        monkeypatch.setattr(claude, "relayed_proxy_base_url", lambda state: "http://127.0.0.1:9999")
        state = {"workspace": WS, "codex_models": []}

        with pytest.raises(RuntimeError, match="Cannot safely inspect"):
            claude.write_tool_config(state, "databricks-claude-sonnet-4", relayed=True)

        assert managed_writes == []

    def test_noninteractive_uses_local_settings_when_managed_file_is_compatible(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: False)
        state = {"workspace": WS, "codex_models": []}

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        assert managed_writes == []

    def test_noninteractive_fails_when_managed_file_conflicts(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing = {
            str(FAKE_MANAGED_PATH): {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com"}}
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: False)
        state = {"workspace": WS, "codex_models": []}

        with pytest.raises(RuntimeError, match="cannot be applied non-interactively"):
            claude.write_tool_config(state, "databricks-claude-sonnet-4")

        assert managed_writes == []

    def test_sudo_failure_uses_local_settings_when_managed_file_is_compatible(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        warnings: list[str] = []
        verified: list[dict] = []
        self._patch(monkeypatch, private_writes, managed_writes)

        def deny_managed_write(*args, **kwargs):
            raise managed_files.ManagedFileWriteUnavailable("sudo denied")

        monkeypatch.setattr(
            claude,
            "reconcile_managed_file",
            deny_managed_write,
        )
        monkeypatch.setattr(claude, "print_warning", warnings.append)
        monkeypatch.setattr(
            claude,
            "mark_managed_file_verified",
            lambda *args, **kwargs: verified.append(kwargs),
        )

        claude.write_tool_config(
            {"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4"
        )

        assert private_writes
        assert managed_writes == []
        assert "continuing with local settings" in warnings[0]
        assert verified == [{"scope": "local-compatible"}]

    def test_sudo_failure_remains_fatal_when_managed_file_conflicts(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        existing = {
            str(FAKE_MANAGED_PATH): {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com"}}
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)

        def deny_managed_write(*args, **kwargs):
            raise managed_files.ManagedFileWriteUnavailable("sudo denied")

        monkeypatch.setattr(
            claude,
            "reconcile_managed_file",
            deny_managed_write,
        )

        with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="sudo denied"):
            claude.write_tool_config(
                {"workspace": WS, "codex_models": []}, "databricks-claude-sonnet-4"
            )

    def test_static_models_written_to_picker(self, monkeypatch):
        # Static models from state are rendered into the managed settings picker.
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        static_models = ["system.ai.claude-opus-4-8", "system.ai.claude-sonnet-4-6"]
        state = {
            "workspace": WS,
            "codex_models": [],
            "claude_static_models": static_models,
        }
        claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        # Managed file should have the picker.
        assert len(managed_writes) > 0
        managed_content = json.loads(managed_writes[0][1])
        assert managed_content["availableModels"] == static_models
        assert managed_content["enforceAvailableModels"] is True
        assert "modelPicker" in managed_content
        assert len(managed_content["modelPicker"]["options"]) == 2

    def test_static_models_not_written_when_absent(self, monkeypatch):
        # When claude_static_models is not in state, picker fields are not written.
        private_writes: list = []
        managed_writes: list = []
        self._patch(monkeypatch, private_writes, managed_writes)
        state = {"workspace": WS, "codex_models": []}
        claude.write_tool_config(state, "databricks-claude-sonnet-4")
        # Managed file should not have the picker.
        assert len(managed_writes) > 0
        managed_content = json.loads(managed_writes[0][1])
        assert "availableModels" not in managed_content
        assert "modelPicker" not in managed_content


class TestAddClaudeMcpServer:
    def test_registers_stdio_proxy_command(self, monkeypatch):
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return MagicMock(returncode=0)

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        claude.add_claude_mcp_server("github", _proxy_argv())

        args = calls[0]["args"]
        assert args[:4] == ["claude", "mcp", "add", "github"]
        assert args[4:6] == ["-s", "user"]
        # `--` fences the proxy argv; everything after it is the stdio command.
        assert args[6] == "--"
        assert args[7:] == _proxy_argv()

    def test_always_load_routes_through_add_json_stdio_entry(self, monkeypatch):
        # The skills registry needs `alwaysLoad: true`, which plain `mcp add`
        # can't set — so the proxy argv is wrapped in a stdio entry dict and
        # registered via add-json instead.
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return MagicMock(returncode=0)

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        claude.add_claude_mcp_server("skills", _proxy_argv(), always_load=True)

        args = calls[0]["args"]
        assert args[:4] == ["claude", "mcp", "add-json", "skills"]
        entry = json.loads(args[4])
        assert entry == {
            "type": "stdio",
            "command": _proxy_argv()[0],
            "args": _proxy_argv()[1:],
            "alwaysLoad": True,
        }
        assert args[5:] == ["-s", "user"]

    def test_dict_entry_routes_through_add_json(self, monkeypatch):
        # The web_search server registers a full stdio entry dict with its own
        # env, which only `add-json` can express — a dict must route there rather
        # than through the proxy `mcp add -- <argv>` path.
        calls: list[dict] = []

        def fake_run(args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return MagicMock(returncode=0)

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        entry = {"type": "stdio", "command": "ucode", "args": ["mcp", "web-search"]}
        claude.add_claude_mcp_server("web_search", entry)

        args = calls[0]["args"]
        assert args[:4] == ["claude", "mcp", "add-json", "web_search"]
        assert json.loads(args[4]) == entry
        assert args[5:] == ["-s", "user"]


class TestRemoveClaudeMcpServer:
    def test_returns_true_when_server_removed(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0)

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        assert claude.remove_claude_mcp_server("github", "user") is True
        assert calls == [["claude", "mcp", "remove", "github", "-s", "user"]]

    def test_returns_false_when_server_missing(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(1, args, stderr="No MCP server named github found")

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        assert claude.remove_claude_mcp_server("github", "user") is False

    def test_returns_false_when_project_local_server_missing(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                1,
                args,
                stderr="No project-local MCP server found with name: github",
            )

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        assert claude.remove_claude_mcp_server("github", "project") is False

    def test_returns_false_when_user_scoped_server_missing(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                1,
                args,
                stderr="No user-scoped MCP server found with name: github",
            )

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        assert claude.remove_claude_mcp_server("github", "user") is False

    def test_unexpected_failure_raises(self, monkeypatch):
        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(1, args, stderr="permission denied")

        monkeypatch.setattr(claude.subprocess, "run", fake_run)

        try:
            claude.remove_claude_mcp_server("github", "user")
        except RuntimeError as exc:
            assert "Failed to remove MCP server 'github'" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")


class TestRegisterWebSearchMcp:
    def test_legacy_ucode_command_requires_reregistration(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.shutil.which", lambda command: f"/tools/{command}")
        entry = claude._web_search_mcp_entry(WS, "m", "profile")
        legacy_entry = {**entry, "command": "/tools/ucode"}
        state = {claude.WEB_SEARCH_MCP_STATE_KEY: legacy_entry}
        monkeypatch.setattr(
            claude,
            "read_json_safe",
            lambda path: {"mcpServers": {claude.WEB_SEARCH_MCP_NAME: legacy_entry}},
        )

        assert claude._web_search_mcp_is_current(state, entry) is False

    def test_skips_registration_when_entry_is_current(self, monkeypatch):
        entry = claude._web_search_mcp_entry(WS, "m", "profile")
        state = {claude.WEB_SEARCH_MCP_STATE_KEY: entry}
        monkeypatch.setattr(
            claude,
            "read_json_safe",
            lambda path: {"mcpServers": {claude.WEB_SEARCH_MCP_NAME: entry}},
        )
        assert claude._web_search_mcp_is_current(state, entry) is True

    def test_detects_registration_drift(self, monkeypatch):
        entry = claude._web_search_mcp_entry(WS, "m", "profile")
        state = {claude.WEB_SEARCH_MCP_STATE_KEY: entry}
        monkeypatch.setattr(claude, "read_json_safe", lambda path: {"mcpServers": {}})
        assert claude._web_search_mcp_is_current(state, entry) is False

    def test_clears_existing_then_adds(self, monkeypatch):
        removed: list[str] = []
        added: list = []
        monkeypatch.setattr(
            claude, "remove_claude_mcp_server", lambda name, scope: removed.append(scope) or True
        )
        monkeypatch.setattr(
            claude,
            "add_claude_mcp_server",
            lambda name, entry, scope=claude.MCP_USER_SCOPE: added.append((name, entry, scope)),
        )
        claude._register_web_search_mcp(WS, "databricks-gpt-5")
        assert removed == list(claude.MCP_CLEANUP_SCOPES)
        assert len(added) == 1
        name, entry, _ = added[0]
        assert name == "web_search"
        assert entry["env"]["UCODE_WEB_SEARCH_MODEL"] == "databricks-gpt-5"

    def test_remove_failures_are_swallowed(self, monkeypatch):
        def boom(name, scope):
            raise RuntimeError("nope")

        added: list = []
        monkeypatch.setattr(claude, "remove_claude_mcp_server", boom)
        monkeypatch.setattr(
            claude,
            "add_claude_mcp_server",
            lambda name, entry, scope=claude.MCP_USER_SCOPE: added.append(name),
        )
        claude._register_web_search_mcp(WS, "m")
        assert added == ["web_search"]

    def test_add_failure_is_non_blocking_and_warns(self, monkeypatch, capsys):
        # Regression: a failing `claude mcp add-json` used to abort the whole
        # `ucode claude` setup. It must now warn and return False instead.
        monkeypatch.setattr(claude, "remove_claude_mcp_server", lambda name, scope: False)

        def boom(name, entry, scope=claude.MCP_USER_SCOPE):
            raise RuntimeError("Failed to add MCP server 'web_search' via claude CLI.")

        monkeypatch.setattr(claude, "add_claude_mcp_server", boom)
        result = claude._register_web_search_mcp(WS, "m")
        assert result is False
        captured = capsys.readouterr()
        assert "web_search" in captured.out.lower() or "web search" in captured.out.lower()

    def test_add_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(claude, "remove_claude_mcp_server", lambda name, scope: False)
        monkeypatch.setattr(
            claude,
            "add_claude_mcp_server",
            lambda name, entry, scope=claude.MCP_USER_SCOPE: None,
        )
        assert claude._register_web_search_mcp(WS, "m") is True

    def test_write_tool_config_completes_when_mcp_registration_fails(self, monkeypatch):
        # Regression for issue #100: a `claude mcp add-json` failure must not
        # block the rest of `ucode claude` setup (state save, managed-key
        # marking, etc.) from completing.
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "read_json_safe", lambda path: {})
        monkeypatch.setattr(claude, "write_json_file", lambda path, payload: None)
        saved: list[dict] = []
        monkeypatch.setattr(claude, "save_state", lambda state: saved.append(state))
        monkeypatch.setattr(claude, "remove_claude_mcp_server", lambda name, scope: False)

        def boom(name, entry, scope=claude.MCP_USER_SCOPE):
            raise RuntimeError("Failed to add MCP server 'web_search' via claude CLI.")

        monkeypatch.setattr(claude, "add_claude_mcp_server", boom)

        state = {"workspace": WS, "codex_models": ["databricks-gpt-5"]}
        result = claude.write_tool_config(state, "databricks-claude-sonnet-4")
        assert saved, "save_state should still be called when MCP registration fails"
        assert result["workspace"] == WS


class TestClaudeLaunch:
    def test_gateway_discovery_enabled_for_relayed_provider(self, monkeypatch):
        calls: list[tuple[dict, str, list[str]]] = []
        monkeypatch.setenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, "1")
        monkeypatch.delenv("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", raising=False)
        monkeypatch.setattr(
            claude,
            "_launch_relayed",
            lambda state, binary, tool_args: calls.append((state, binary, tool_args)),
        )
        state = {"workspace": WS, "claude_relayed": True}

        claude.launch(state, ["--debug"], options=LaunchOptions())

        assert os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
        assert calls == [(state, "claude", ["--debug"])]

    def test_relayed_launch_uses_refresh_proxy(self, monkeypatch):
        calls: list[tuple] = []

        class Server:
            server_address = ("127.0.0.1", 12345)

        class Process:
            def __init__(self, argv):
                calls.append(("popen", argv))

            def wait(self):
                return 0

        @contextlib.contextmanager
        def running_proxy(workspace, token_provider, port, spec, *, force_refresh_near_expiry):
            # Record how the proxy is configured, and resolve the provider once to
            # prove Claude authenticates the proxy with its own workspace/profile.
            calls.append(
                (
                    "proxy",
                    workspace,
                    port,
                    spec,
                    force_refresh_near_expiry,
                    token_provider(False),
                )
            )
            yield Server()
            calls.append(("torn_down",))

        monkeypatch.setattr(claude, "_managed_relayed_conflicts", lambda: None)
        monkeypatch.setattr(claude, "_ensure_subscription_login", lambda: None)
        monkeypatch.setattr(claude.gateway_proxy, "running_proxy", running_proxy)
        monkeypatch.setattr(
            claude,
            "get_databricks_token",
            lambda ws, profile, force_refresh=False: f"tok:{ws}:{profile}:{force_refresh}",
        )
        monkeypatch.setattr(claude.subprocess, "Popen", Process)

        with pytest.raises(SystemExit) as exc:
            claude.launch(
                {
                    "workspace": WS,
                    "profile": "test",
                    "claude_relayed": True,
                    "relayed_proxy_port": 12345,
                },
                ["--debug"],
                options=LaunchOptions(),
            )

        assert exc.value.code == 0
        assert calls[0] == (
            "proxy",
            WS,
            12345,
            claude.gateway_proxy.RELAY_SPEC,
            False,
            f"tok:{WS}:test:False",  # provider uses Claude's own workspace + profile
        )
        assert ("popen", ["claude", "--debug"]) in calls or any(c[0] == "popen" for c in calls)
        assert calls[-1] == ("torn_down",)  # proxy always torn down

    def test_smart_routing_on_windows_is_not_supported(self, monkeypatch):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(claude.os, "name", "nt")

        with pytest.raises(
            RuntimeError,
            match="Smart routing in Claude Code is currently not supported on Windows",
        ):
            claude.launch(
                {"workspace": WS, "profile": "test"},
                ["--debug"],
                options=LaunchOptions(launch_smart_routing=True),
            )

    def test_default_launch_keeps_existing_auth_path(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.delenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
        monkeypatch.delenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, raising=False)
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))

        claude.launch({"workspace": WS, "profile": "test"}, ["--debug"], options=LaunchOptions())

        assert os.environ["OAUTH_TOKEN"] == "token"
        assert calls == [["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH), "--debug"]]

    def test_launch_model_is_only_set_for_current_process(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))

        claude.launch(
            {"workspace": WS, "profile": "test"},
            [],
            options=LaunchOptions(user_pinned_model="cat.schema.model"),
        )

        assert os.environ["ANTHROPIC_MODEL"] == "cat.schema.model"
        assert calls[0][:2] == ["claude", "--settings"]
        settings = json.loads(calls[0][2])
        assert settings["env"]["ANTHROPIC_MODEL"] == "cat.schema.model"

    @pytest.mark.parametrize(
        "tool_args",
        [
            ["--print", "say hi"],
            ["doctor"],
        ],
    )
    def test_v2_noninteractive_launch_bypasses_first_prompt_routing(self, monkeypatch, tool_args):
        calls: list[list[str]] = []
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        monkeypatch.setattr(v2, "launch_claude", Mock())
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))

        claude.launch({"workspace": WS}, tool_args, options=LaunchOptions())

        assert calls == [["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH), *tool_args]]
        v2.launch_claude.assert_not_called()

    @pytest.mark.parametrize("tool_args", [["fix this bug"], ["--", "fix this bug"]])
    def test_v2_positional_prompt_uses_first_prompt_routing(self, monkeypatch, tool_args):
        monkeypatch.setenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, "1")
        launch_v2 = Mock()
        monkeypatch.setattr(v2, "launch_claude", launch_v2)

        claude.launch(
            {"workspace": WS},
            tool_args,
            options=LaunchOptions(launch_smart_routing=True),
        )

        launch_v2.assert_called_once_with(
            {"workspace": WS},
            tool_args,
            binary="claude",
            user_settings_path=claude.CLAUDE_USER_SETTINGS_PATH,
            launch_model=None,
            compose_settings=claude._compose_v2_settings,
            launch_model_args=claude._launch_model_args,
            model_name=claude._maybe_add_1m_suffix,
        )

    def test_gateway_discovery_uses_direct_gateway(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.delenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
        monkeypatch.setenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, "1")
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))

        claude.launch({"workspace": WS, "profile": "test"}, ["--debug"], options=LaunchOptions())

        assert os.environ["OAUTH_TOKEN"] == "token"
        assert os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
        assert calls == [["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH), "--debug"]]

    def test_gateway_discovery_enabled_under_provider(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.delenv(v2.ENABLE_SMART_ROUTING_ENV_VAR, raising=False)
        monkeypatch.setenv(claude.GATEWAY_MODEL_DISCOVERY_ENV_VAR, "1")
        monkeypatch.delenv("OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(claude, "get_databricks_token", lambda *_args: "token")
        monkeypatch.setattr(claude, "exec_or_spawn", lambda argv: calls.append(argv))

        claude.launch(
            {
                "workspace": WS,
                "profile": "test",
                "_claude_launch_provider": "main.default.anthropic",
            },
            ["--debug"],
            options=LaunchOptions(),
        )

        assert os.environ["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
        assert calls == [["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH), "--debug"]]


class TestWriteToolConfigPrunesStaleModelEnv:
    """Stale ucode-managed model env keys (ANTHROPIC_MODEL, etc.) from earlier
    ucode versions must be removed on every launch — otherwise they linger in
    settings.json and re-introduce the duplicate /model picker row that this
    change is meant to remove.
    """

    def _patch(self, monkeypatch, existing_settings):
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "read_json_safe", lambda path: existing_settings)
        written: dict = {}

        def fake_write(path, payload):
            written["payload"] = payload

        monkeypatch.setattr(claude, "write_json_file", fake_write)
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
        return written

    def test_prunes_stale_anthropic_model_from_prior_run(self, monkeypatch):
        existing = {
            "env": {
                "ANTHROPIC_MODEL": "system.ai.claude-opus-4-8[1m]",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8[1m]",
                "MY_CUSTOM_VAR": "keep-me",
            }
        }
        written = self._patch(monkeypatch, existing)
        state = {
            "workspace": WS,
            "claude_models": {"opus": "system.ai.claude-opus-4-8"},
        }
        claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        env = written["payload"]["env"]
        assert "ANTHROPIC_MODEL" not in env
        # Family default we still write this run is preserved.
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"
        # User-owned keys are untouched.
        assert env["MY_CUSTOM_VAR"] == "keep-me"

    def test_prunes_unused_family_default_when_models_change(self, monkeypatch):
        existing = {
            "env": {
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-6[1m]",
            }
        }
        written = self._patch(monkeypatch, existing)
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}
        claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        env = written["payload"]["env"]
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"

    def test_prunes_stale_name_companion_keys_from_older_ucode(self, monkeypatch):
        # An older ucode build briefly wrote `_NAME` companion env vars to give
        # the picker friendly labels. The current build only writes the raw id,
        # so any leftover `_NAME` keys must be pruned — otherwise users who
        # tested the in-between version would see stale labels.
        existing = {
            "env": {
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8[1m]",
                "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Opus 4.8 (1M)",
                "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "Sonnet 4.6 (1M)",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "Haiku 4.5",
            }
        }
        written = self._patch(monkeypatch, existing)
        state = {"workspace": WS, "claude_models": {"opus": "system.ai.claude-opus-4-8"}}
        claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        env = written["payload"]["env"]
        assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "system.ai.claude-opus-4-8[1m]"
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME" not in env
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME" not in env
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME" not in env


class TestBuildClaudeArgv:
    def test_no_caller_settings_uses_ucode_file(self, monkeypatch):
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        argv = claude._build_claude_argv("claude", ["-p", "hi"])
        assert argv == ["claude", "--settings", str(claude.CLAUDE_SETTINGS_PATH), "-p", "hi"]

    def test_non_relayed_does_not_set_setting_sources(self, monkeypatch):
        # Normal launches must keep loading user settings (hooks/permissions) —
        # no --setting-sources so nothing changes for the stored-key path.
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        argv = claude._build_claude_argv("claude", ["-p", "hi"], relayed=False)
        assert "--setting-sources" not in argv

    def test_relayed_excludes_user_scope_via_setting_sources(self, monkeypatch):
        # Relayed must drop the user scope so a stale ~/.claude/settings.json
        # apiKeyHelper can't merge through and shadow the subscription OAuth.
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"env": {}})
        argv = claude._build_claude_argv("claude", ["-p", "hi"], relayed=True)
        assert "--setting-sources" in argv
        src = argv[argv.index("--setting-sources") + 1]
        assert src == claude._RELAYED_SETTING_SOURCES
        assert "user" not in src
        # ucode's own settings file is still passed.
        assert "--settings" in argv
        assert str(claude.CLAUDE_SETTINGS_PATH) in argv

    def test_relayed_with_caller_settings_keeps_setting_sources(self, monkeypatch):
        # Even when composing a caller --settings, relayed still excludes user scope.
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"env": {}})
        caller = json.dumps({"statusLine": {"type": "command", "command": "sl"}})
        argv = claude._build_claude_argv("claude", ["--settings", caller], relayed=True)
        assert argv[:3] == ["claude", "--setting-sources", claude._RELAYED_SETTING_SOURCES]
        assert argv.count("--settings") == 1

    def test_inline_caller_settings_merged_into_single_flag(self, monkeypatch):
        ucode_settings = {
            "apiKeyHelper": "ucode-helper",
            "env": {"ANTHROPIC_BASE_URL": "https://gw"},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "ucode-stop"}]}]},
        }
        monkeypatch.setattr(claude, "read_json_safe", lambda p: ucode_settings)
        caller = json.dumps(
            {
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "caller-stop"}]}]},
                "statusLine": {"type": "command", "command": "sl"},
            }
        )
        argv = claude._build_claude_argv("claude", ["--settings", caller, "-p", "hi"])
        # Exactly one --settings reaches Claude, and the caller's raw flag is gone.
        assert argv.count("--settings") == 1
        assert argv[:2] == ["claude", "--settings"]
        assert argv[3:] == ["-p", "hi"]
        merged = json.loads(argv[2])
        # ucode's gateway config survives.
        assert merged["apiKeyHelper"] == "ucode-helper"
        assert merged["env"]["ANTHROPIC_BASE_URL"] == "https://gw"
        # The caller's own (non-hook) settings pass through.
        assert merged["statusLine"] == {"type": "command", "command": "sl"}
        # Hooks from BOTH sides fire (unioned, not clobbered).
        stop_cmds = [h["command"] for e in merged["hooks"]["Stop"] for h in e["hooks"]]
        assert "ucode-stop" in stop_cmds
        assert "caller-stop" in stop_cmds

    def test_equals_form_is_handled(self, monkeypatch):
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        caller = json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "c"}]}]}})
        argv = claude._build_claude_argv("claude", [f"--settings={caller}"])
        assert argv.count("--settings") == 1
        merged = json.loads(argv[2])
        assert merged["apiKeyHelper"] == "u"
        assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "c"

    def test_ucode_wins_on_conflicting_env(self, monkeypatch):
        monkeypatch.setattr(
            claude, "read_json_safe", lambda p: {"env": {"ANTHROPIC_BASE_URL": "https://ucode"}}
        )
        caller = json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://caller", "FOO": "bar"}})
        argv = claude._build_claude_argv("claude", ["--settings", caller])
        merged = json.loads(argv[2])
        assert merged["env"]["ANTHROPIC_BASE_URL"] == "https://ucode"  # ucode wins
        assert merged["env"]["FOO"] == "bar"  # caller's non-conflicting key kept

    def test_file_path_caller_settings(self, tmp_path, monkeypatch):
        caller_file = tmp_path / "caller.json"
        caller_file.write_text(
            json.dumps(
                {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "cs"}]}]}}
            )
        )
        # The caller file is read directly; read_json_safe is only used for
        # ucode's own settings file.
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        argv = claude._build_claude_argv("claude", ["--settings", str(caller_file)])
        assert argv.count("--settings") == 1
        merged = json.loads(argv[2])
        assert merged["apiKeyHelper"] == "u"
        assert merged["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "cs"

    def test_malformed_inline_json_raises(self, monkeypatch):
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        # Clearly-intended-as-JSON but broken: fail loudly rather than pass it
        # through as a second, colliding --settings flag.
        with pytest.raises(RuntimeError, match="not valid JSON"):
            claude._build_claude_argv("claude", ["--settings", '{"hooks": '])

    def test_nonexistent_file_raises(self, monkeypatch):
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        with pytest.raises(RuntimeError, match="file not found"):
            claude._build_claude_argv("claude", ["--settings", "/no/such/settings.json"])

    def test_non_object_file_json_raises(self, tmp_path, monkeypatch):
        # A --settings file whose JSON is not an object (e.g. an array) can't be
        # merged; fail loudly. (An inline value only enters the JSON branch when
        # it starts with "{", so the non-object case is reachable via a file.)
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("[1, 2, 3]")
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        with pytest.raises(RuntimeError, match="must be a JSON object"):
            claude._build_claude_argv("claude", ["--settings", str(bad_file)])

    def test_malformed_file_json_raises(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text('{"hooks": ')
        monkeypatch.setattr(claude, "read_json_safe", lambda p: {"apiKeyHelper": "u"})
        with pytest.raises(RuntimeError, match="not valid JSON"):
            claude._build_claude_argv("claude", ["--settings", str(bad_file)])


class TestClaudeSmartRouting:
    def _capture_write(self, monkeypatch, existing, written):
        monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "read_json_safe", lambda path: existing)
        monkeypatch.setattr(
            claude, "write_json_file", lambda path, payload: written.append(payload)
        )
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)

    def test_write_config_removes_legacy_routing_hooks(self, monkeypatch):
        written: list = []
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"command": "user-policy"}]},
                    {
                        "matcher": "Agent|Task",
                        "hooks": [{"command": "ucode claude-router-hook route-subagent"}],
                    },
                ]
            }
        }
        self._capture_write(monkeypatch, existing, written)
        state = {
            "workspace": WS,
            "claude_models": {"opus": "system.ai.claude-opus-4-8"},
            claude.SMART_ROUTING_STATE_KEY: True,
        }
        claude.write_tool_config(state, "system.ai.claude-opus-4-8", route_root_model=None)
        assert written[0]["hooks"]["PreToolUse"] == [
            {"matcher": "Bash", "hooks": [{"command": "user-policy"}]}
        ]

    def test_root_model_pins_anthropic_model(self, monkeypatch):
        written: list = []
        self._capture_write(monkeypatch, {}, written)
        state = {
            "workspace": WS,
            "claude_models": {"opus": "system.ai.claude-opus-4-8"},
            claude.SMART_ROUTING_STATE_KEY: True,
        }
        claude.write_tool_config(
            state, "system.ai.claude-opus-4-8", route_root_model="system.ai.claude-sonnet-5"
        )
        assert written[0]["env"]["ANTHROPIC_MODEL"] == "system.ai.claude-sonnet-5"

    def test_provider_suppresses_routing_hooks(self, monkeypatch):
        written: list = []
        self._capture_write(monkeypatch, {}, written)
        state = {"workspace": WS, claude.SMART_ROUTING_STATE_KEY: True}
        # Under a Model Provider Service no Databricks model is pinned, so routing
        # is inapplicable — hooks must not be installed even when the flag is set.
        claude.write_tool_config(state, None, provider="cat.sch.svc")
        assert "hooks" not in written[0] or "PreToolUse" not in written[0]["hooks"]

    def test_disable_removes_only_ucode_hooks(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "ucode-settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "user-policy"}],
                            },
                            {
                                "matcher": "Agent|Task",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "ucode claude-router-hook route-subagent",
                                    }
                                ],
                            },
                        ],
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "ucode claude-router-hook session-start",
                                    }
                                ]
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", settings_path)
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude_routing, "clear_routing_artifacts", lambda: None)
        state = {"workspace": WS, claude.SMART_ROUTING_STATE_KEY: True}

        assert claude.disable_smart_routing(state) is True

        doc = json.loads(settings_path.read_text())
        assert state.get(claude.SMART_ROUTING_STATE_KEY) is None
        assert list(doc["hooks"]) == ["PreToolUse"]
        assert doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "user-policy"


class TestEnsureSubscriptionLogin:
    """Relayed launch's subscription-login gate."""

    @staticmethod
    def _forbid_subprocess(monkeypatch):
        """Fail loudly if the CLI is shelled out to at all (status probe or login)."""

        def _boom(*args, **kwargs):
            raise AssertionError(f"unexpected subprocess call: {args!r}")

        monkeypatch.setattr(claude.subprocess, "run", _boom)

    def test_oauth_token_env_skips_login(self, monkeypatch):
        # A pre-provisioned CLAUDE_CODE_OAUTH_TOKEN (e.g. `claude setup-token`
        # output in CI) is the credential Claude Code uses directly, so no
        # interactive browser login applies — and no `auth status` probe is even
        # needed. This keeps headless/relayed runs from hanging on the browser.
        monkeypatch.setenv(claude.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, "dummy-oauth-token")
        self._forbid_subprocess(monkeypatch)
        claude._ensure_subscription_login()  # returns without touching the CLI

    def test_existing_login_skips_browser(self, monkeypatch):
        monkeypatch.delenv(claude.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, raising=False)
        monkeypatch.setattr(claude, "_has_subscription_login", lambda: True)

        def _boom(cmd, **kwargs):
            raise AssertionError(f"no auth login expected, got {cmd!r}")

        monkeypatch.setattr(claude.subprocess, "run", _boom)
        claude._ensure_subscription_login()

    def test_missing_login_runs_browser_flow(self, monkeypatch):
        monkeypatch.delenv(claude.CLAUDE_CODE_OAUTH_TOKEN_ENV_VAR, raising=False)
        monkeypatch.setattr(claude, "_has_subscription_login", lambda: False)
        calls: list[list[str]] = []
        monkeypatch.setattr(claude.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))
        monkeypatch.setattr(claude, "print_note", lambda *a, **kw: None)
        monkeypatch.setattr(claude, "print_success", lambda *a, **kw: None)
        claude._ensure_subscription_login()
        assert calls == [[claude.SPEC["binary"], "auth", "login"]]


class TestWriteToolConfigBackup:
    """A re-configure must not snapshot the file ucode itself generated."""

    def _patch(self, monkeypatch, tmp_path):
        monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", tmp_path / "ucode-settings.json")
        monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", tmp_path / "backup.json")
        monkeypatch.setattr("ucode.config_io.APP_DIR", tmp_path)
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: None)

    def test_first_configure_backs_up_user_owned_file(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path)
        (tmp_path / "ucode-settings.json").write_text(
            '{"permissions": {"allow": ["Read"]}}', encoding="utf-8"
        )

        claude.write_tool_config(
            {"workspace": WS, "claude_models": {}}, "databricks-claude-sonnet-4"
        )

        backup = (tmp_path / "backup.json").read_text(encoding="utf-8")
        assert backup == '{"permissions": {"allow": ["Read"]}}'

    def test_reconfigure_does_not_back_up_generated_file(self, tmp_path, monkeypatch):
        self._patch(monkeypatch, tmp_path)
        state = {
            "workspace": WS,
            "claude_models": {},
            # load_state after a first configure: ucode already manages this file.
            "managed_configs": {"claude": {"keys": [["env", "ANTHROPIC_BASE_URL"]]}},
        }

        claude.write_tool_config(state, "databricks-claude-sonnet-4")

        assert not (tmp_path / "backup.json").exists()


class TestManagedMcpUsesManagedFile:
    def _wire(
        self, monkeypatch, *, supported=True, interactive=True, version="2.1.259", oauth=True
    ):
        monkeypatch.setattr(claude, "managed_files_supported", lambda: supported)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: interactive)
        monkeypatch.setattr(claude, "agent_version", lambda _binary: version)
        monkeypatch.setattr(claude, "oauth_client_available", lambda ws, client_id: oauth)

    def test_true_when_all_conditions_hold(self, monkeypatch):
        self._wire(monkeypatch)
        assert claude.managed_mcp_uses_managed_file(WS, use_pat=False) is True

    def test_false_on_old_claude(self, monkeypatch):
        self._wire(monkeypatch, version="2.1.258")
        assert claude.managed_mcp_uses_managed_file(WS, use_pat=False) is False

    def test_false_under_pat(self, monkeypatch):
        self._wire(monkeypatch)
        assert claude.managed_mcp_uses_managed_file(WS, use_pat=True) is False

    def test_false_without_oauth_client(self, monkeypatch):
        self._wire(monkeypatch, oauth=False)
        assert claude.managed_mcp_uses_managed_file(WS, use_pat=False) is False

    def test_false_when_non_interactive(self, monkeypatch):
        self._wire(monkeypatch, interactive=False)
        assert claude.managed_mcp_uses_managed_file(WS, use_pat=False) is False


class TestClaudeReconcileManagedMcp:
    def _wire(self, monkeypatch, existing_text, captured):
        monkeypatch.setattr(
            claude, "_managed_settings_path", lambda: Path("/etc/claude-code/managed-settings.json")
        )
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(claude, "read_managed_file", lambda path: existing_text)
        monkeypatch.setattr(claude, "mark_managed_file_verified", lambda *a, **k: None)

        def fake_reconcile(path, desired_text, *, tool, display, owned_paths):
            captured.update(text=desired_text, tool=tool, owned_paths=owned_paths)

        monkeypatch.setattr(claude, "reconcile_managed_file", fake_reconcile)

    def test_writes_managed_mcp_servers_preserving_other_keys(self, monkeypatch):
        captured: dict = {}
        existing = json.dumps({"env": {"X": "1"}, "apiKeyHelper": "ug auth-token"})
        self._wire(monkeypatch, existing, captured)
        used = claude.reconcile_managed_mcp(
            {}, {"system-ai-github": claude.managed_mcp_entry(GH_URL)}
        )
        assert used is True
        doc = json.loads(captured["text"])
        assert doc["managedMcpServers"]["system-ai-github"]["url"] == GH_URL
        assert doc["managedMcpServers"]["system-ai-github"]["type"] == "http"
        assert doc["managedMcpServers"]["system-ai-github"]["oauth"]["clientId"] == "claude-code"
        assert doc["env"] == {"X": "1"}
        assert doc["apiKeyHelper"] == "ug auth-token"
        assert captured["owned_paths"] == [["managedMcpServers"]]
        assert captured["tool"] == "claude"

    def test_empty_map_clears_key_preserving_other_keys(self, monkeypatch):
        captured: dict = {}
        existing = json.dumps(
            {"managedMcpServers": {"old": {"type": "http", "url": "u"}}, "env": {"X": "1"}}
        )
        self._wire(monkeypatch, existing, captured)
        used = claude.reconcile_managed_mcp({}, {})
        assert used is True
        doc = json.loads(captured["text"])
        assert "managedMcpServers" not in doc
        assert doc["env"] == {"X": "1"}

    def test_clearing_an_absent_key_never_writes(self, monkeypatch):
        # No managedMcpServers to clear (and possibly no file): must not create or rewrite anything.
        monkeypatch.setattr(
            claude, "_managed_settings_path", lambda: Path("/etc/claude-code/managed-settings.json")
        )
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(claude, "read_managed_file", lambda path: None)
        monkeypatch.setattr(claude, "mark_managed_file_verified", lambda *a, **k: None)
        monkeypatch.setattr(
            claude, "reconcile_managed_file", lambda *a, **k: pytest.fail("must not write")
        )
        assert claude.reconcile_managed_mcp({}, {}) is True

    def test_non_interactive_returns_false_without_writing(self, monkeypatch):
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: Path("/etc/x.json"))
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: False)
        monkeypatch.setattr(
            claude, "reconcile_managed_file", lambda *a, **k: pytest.fail("must not write")
        )
        assert claude.reconcile_managed_mcp({}, {"s": claude.managed_mcp_entry(GH_URL)}) is False

    def test_preserves_prior_verification_scope(self, monkeypatch):
        # An MCP-only write must refresh the fingerprint without downgrading the model reconcile's
        # scope (e.g. relay-compatible), or a relayed launch would re-reconcile and status would drift.
        captured: dict = {}
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: Path("/etc/x.json"))
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(claude, "read_managed_file", lambda path: json.dumps({"env": {}}))
        monkeypatch.setattr(claude, "reconcile_managed_file", lambda *a, **k: None)

        def fake_mark(state, tool, path, *, scope="managed"):
            captured["scope"] = scope

        monkeypatch.setattr(claude, "mark_managed_file_verified", fake_mark)
        state = {"managed_file_fingerprints": {"claude": {"scope": "relay-compatible"}}}
        claude.reconcile_managed_mcp(state, {"gh": claude.managed_mcp_entry(GH_URL)})
        assert captured["scope"] == "relay-compatible"


class TestClaudeReadManagedMcpUrls:
    def test_reads_urls_from_managed_file(self, monkeypatch):
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: Path("/etc/x.json"))
        text = json.dumps({"managedMcpServers": {"gh": {"type": "http", "url": GH_URL}}, "env": {}})
        monkeypatch.setattr(claude, "read_managed_file", lambda path: text)
        assert claude.read_managed_mcp_urls() == {"gh": GH_URL}

    def test_empty_when_key_absent(self, monkeypatch):
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: Path("/etc/x.json"))
        monkeypatch.setattr(claude, "read_managed_file", lambda path: json.dumps({"env": {}}))
        assert claude.read_managed_mcp_urls() == {}

    def test_empty_when_file_unreadable(self, monkeypatch):
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: Path("/etc/x.json"))

        def boom(path):
            raise RuntimeError("permission denied")

        monkeypatch.setattr(claude, "read_managed_file", boom)
        assert claude.read_managed_mcp_urls() == {}
