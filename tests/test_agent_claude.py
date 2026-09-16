"""Tests for agents/claude.py."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from ucode import managed_files
from ucode.agents import LaunchOptions, claude
from ucode.smart_routing import claude_routing, v2
from ucode.state import MANAGED_OVERLAY_KEY, developer_state_from_resolved

WS = "https://example.databricks.com"
# A connection MCP proxy argv, used by the Claude MCP-registration helper tests.
# The leading element is the resolved `ug` binary path, so tests assert the tail.
GH_URL = f"{WS}/api/2.0/mcp/external/github"


def _proxy_argv() -> list[str]:
    from ucode.databricks import build_mcp_proxy_argv

    return build_mcp_proxy_argv(GH_URL, WS, "p")


def _patch_private_json_store(monkeypatch, initial: dict, on_write=None) -> dict[str, dict]:
    """Mock the strict private settings reader and its matching atomic writer."""
    store = {str(claude.CLAUDE_SETTINGS_PATH): json.loads(json.dumps(initial))}

    def read(path):
        return json.loads(json.dumps(store.get(str(path), {})))

    def write(path, payload):
        copied = json.loads(json.dumps(payload))
        store[str(path)] = copied
        if on_write is not None:
            on_write(path, copied)

    monkeypatch.setattr(claude, "_read_private_json_object", read)
    monkeypatch.setattr(claude, "write_json_file", write)
    return store


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


def test_picker_process_lock_uses_msvcrt_on_windows(tmp_path, monkeypatch):
    metadata_path = tmp_path / "claude-picker-management.json"
    calls: list[tuple[int, int]] = []
    fake_msvcrt = SimpleNamespace(
        LK_LOCK=11,
        LK_UNLCK=12,
        locking=lambda _fd, mode, size: calls.append((mode, size)),
    )
    monkeypatch.setattr(claude, "CLAUDE_PICKER_MANAGEMENT_PATH", metadata_path)
    monkeypatch.setattr(claude, "current_os", lambda: claude.OS.WINDOWS)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    with claude._picker_process_lock():
        assert metadata_path.with_name(f"{metadata_path.name}.lock").read_bytes() == b"\0"

    assert calls == [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)]


def test_process_file_lock_blocks_a_second_process(tmp_path):
    lock_path = tmp_path / "registration.lock"
    ready_path = tmp_path / "child-ready"
    acquired_path = tmp_path / "child-acquired"
    script = "\n".join(
        [
            "from pathlib import Path",
            "from ucode.agents.claude import _process_file_lock",
            f"lock_path = Path({str(lock_path)!r})",
            f"Path({str(ready_path)!r}).write_text('ready')",
            "with _process_file_lock(lock_path):",
            f"    Path({str(acquired_path)!r}).write_text('acquired')",
        ]
    )

    with claude._process_file_lock(lock_path):
        child = subprocess.Popen([sys.executable, "-c", script])
        deadline = time.monotonic() + 5
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists()
        time.sleep(0.1)
        assert not acquired_path.exists()

    assert child.wait(timeout=5) == 0
    assert acquired_path.read_text() == "acquired"


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
        overlay, _ = claude.render_overlay(
            WS,
            None,
            claude_models={
                "opus": "system.ai.claude-opus-4-8",
                "sonnet": "system.ai.claude-sonnet-4-6",
            },
            parent_schema="main.default",
        )
        assert (
            "Databricks-Model-Service-Parent-Schema: main.default"
            in overlay["env"]["ANTHROPIC_CUSTOM_HEADERS"]
        )
        assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in overlay["env"]
        assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in overlay["env"]

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
        assert overlay["modelPicker"]["options"][0]["label"] == "claude-opus-4-8"

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

    def test_static_models_label_strips_system_ai_prefix(self):
        # Picker labels show the model id without the ``system.ai.`` prefix.
        static = ["system.ai.claude-opus-4-8", "databricks-custom-model"]
        overlay, _ = claude.render_overlay(WS, "s4", static_models=static)
        labels = [opt["label"] for opt in overlay["modelPicker"]["options"]]
        assert labels == ["claude-opus-4-8", "databricks-custom-model"]


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
        _patch_private_json_store(monkeypatch, {})
        persisted: dict = {}

        def save(state):
            persisted.clear()
            persisted.update(json.loads(json.dumps(state)))

        monkeypatch.setattr(claude, "save_state", save)
        monkeypatch.setattr(claude, "load_state", lambda: json.loads(json.dumps(persisted)))
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
        _patch_private_json_store(
            monkeypatch, existing, lambda _path, payload: written.append(payload)
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

        def fake_write_private(path, payload):
            existing_by_path[str(path)] = payload
            private_writes.append((str(path), payload))

        private_initial = existing_by_path.get(str(claude.CLAUDE_SETTINGS_PATH), {})
        private_store = _patch_private_json_store(monkeypatch, private_initial, fake_write_private)
        existing_by_path[str(claude.CLAUDE_SETTINGS_PATH)] = private_store[
            str(claude.CLAUDE_SETTINGS_PATH)
        ]
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
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
            existing_by_path[str(path)] = json.loads(text)
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

    @pytest.mark.parametrize(
        "source_kwargs",
        [
            {"provider": "main.default.anthropic"},
            {"parent_schema": "main.managed_models"},
        ],
        ids=["provider", "model-location"],
    )
    def test_native_discovery_removes_previously_managed_static_picker(
        self, monkeypatch, source_kwargs
    ):
        private_writes: list = []
        managed_writes: list = []
        stale_picker = {
            "availableModels": ["system.ai.claude-opus-4-8"],
            "enforceAvailableModels": True,
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [
                    {
                        "model": "system.ai.claude-opus-4-8",
                        "label": "claude-opus-4-8",
                    }
                ],
            },
        }
        existing_settings = {
            **stale_picker,
            "companyPolicy": {"keep": True},
            "env": {"MY_OWN": "keep"},
        }
        existing = {
            str(claude.CLAUDE_SETTINGS_PATH): existing_settings,
            str(FAKE_MANAGED_PATH): existing_settings,
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)

        def restore_managed_picker(tool, path, current, candidate_paths, **kwargs):
            for candidate_path in candidate_paths:
                current.pop(candidate_path[0], None)
            return current, candidate_paths

        monkeypatch.setattr(claude, "restore_unchanged_managed_paths", restore_managed_picker)
        monkeypatch.setattr(
            claude,
            "managed_last_applied_paths",
            lambda tool, path, candidate_paths, **kwargs: (
                existing_settings,
                candidate_paths,
            ),
        )
        state = {
            "workspace": WS,
            "codex_models": [],
            "claude_static_models": stale_picker["availableModels"],
            "managed_configs": {
                "claude": {"keys": [[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS]}
            },
        }

        result = claude.write_tool_config(state, None, **source_kwargs)

        written_settings = [private_writes[0][1], json.loads(managed_writes[0][1])]
        for written in written_settings:
            assert not set(claude.CLAUDE_MANAGED_PICKER_KEYS) & written.keys()
            assert written["companyPolicy"] == {"keep": True}
            assert written["env"]["MY_OWN"] == "keep"
        assert not any(
            [key] in result["managed_configs"]["claude"]["keys"]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        )

    def test_model_location_preserves_unowned_picker_settings(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        picker_settings = {
            "availableModels": ["enterprise-model"],
            "enforceAvailableModels": True,
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [{"model": "enterprise-model", "label": "Enterprise"}],
            },
        }
        existing = {
            str(claude.CLAUDE_SETTINGS_PATH): picker_settings,
            str(FAKE_MANAGED_PATH): picker_settings,
        }
        self._patch(monkeypatch, private_writes, managed_writes, existing)
        state = {
            "workspace": WS,
            "codex_models": [],
            "managed_configs": {"claude": {"keys": [["env", "ANTHROPIC_BASE_URL"]]}},
        }

        claude.write_tool_config(state, None, parent_schema="main.managed_models")

        assert {
            key: private_writes[0][1][key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        } == picker_settings
        managed = json.loads(managed_writes[0][1])
        assert {key: managed[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker_settings

    def test_managed_file_preserves_picker_without_global_ownership_proof(self, monkeypatch):
        private_writes: list = []
        managed_writes: list = []
        picker_settings = {
            "availableModels": ["enterprise-model"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-model"}]},
        }
        self._patch(
            monkeypatch,
            private_writes,
            managed_writes,
            {str(FAKE_MANAGED_PATH): picker_settings},
        )
        state = {
            "workspace": WS,
            "codex_models": [],
            "managed_configs": {
                "claude": {"keys": [[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS]}
            },
        }

        claude.write_tool_config(state, None, parent_schema="main.managed_models")

        managed = json.loads(managed_writes[0][1])
        assert {key: managed[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker_settings

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

    def test_managed_file_omits_workspace_defaults_for_model_location(self, monkeypatch):
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

        claude.write_tool_config(state, None, parent_schema="main.managed_models")

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
        result = claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        # Managed file should have the picker.
        assert len(managed_writes) > 0
        managed_content = json.loads(managed_writes[0][1])
        assert managed_content["availableModels"] == static_models
        assert managed_content["enforceAvailableModels"] is True
        assert "modelPicker" in managed_content
        assert len(managed_content["modelPicker"]["options"]) == 2
        assert all(
            [key] in result["managed_configs"]["claude"]["keys"]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        )

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


class TestPickerOwnershipAcrossWorkspaces:
    @staticmethod
    def _patch_files(monkeypatch, tmp_path):
        private_path = tmp_path / "ucode-settings.json"
        managed_path = tmp_path / "managed-settings.json"
        backup_path = tmp_path / "ucode-settings.backup.json"
        metadata_path = tmp_path / "claude-picker-management.json"
        monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", private_path)
        monkeypatch.setattr(claude, "CLAUDE_BACKUP_PATH", backup_path)
        monkeypatch.setattr(claude, "CLAUDE_PICKER_MANAGEMENT_PATH", metadata_path)
        monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(managed_files, "managed_writes_allowed", lambda: True)
        monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda path, text: path.write_text(text, encoding="utf-8"),
        )
        monkeypatch.setattr(claude, "save_state", lambda state: None)
        monkeypatch.setattr(claude, "_register_web_search_mcp", lambda *a, **kw: True)
        return private_path, managed_path, backup_path, metadata_path

    def test_scoped_configuration_without_picker_lease_preserves_it_policy(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        picker = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(picker), encoding="utf-8")

        claude.write_tool_config(
            {"workspace": "https://workspace.example.com", "codex_models": []},
            None,
            parent_schema="main.models",
        )

        for path in (private_path, managed_path):
            settings = json.loads(path.read_text())
            assert {key: settings[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker
        assert not metadata_path.exists()
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        assert not any(
            [key] in manifest["files"]["claude"]["owned_paths"]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        )

    def test_legacy_private_backup_without_managed_proof_preserves_current_picker(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        backup_picker = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        current_picker = {
            "availableModels": ["enterprise-two"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-two"}]},
        }
        private_path.write_text(json.dumps(current_picker), encoding="utf-8")
        managed_path.write_text(json.dumps(current_picker), encoding="utf-8")
        backup_path.write_text(json.dumps(backup_picker), encoding="utf-8")
        monkeypatch.setattr(
            claude,
            "load_full_state",
            lambda: {
                "workspaces": {
                    "https://workspace-a.example.com": {
                        "managed_configs": {
                            "claude": {"keys": [[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS]}
                        }
                    }
                }
            },
        )

        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.models",
        )

        written = json.loads(private_path.read_text())
        assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == current_picker
        assert json.loads(backup_path.read_text()) == backup_picker
        assert not metadata_path.exists()

    @pytest.mark.parametrize(
        ("contents", "message"),
        [("{", "Cannot parse Claude settings"), ("[]", "must contain a JSON object")],
        ids=["invalid", "non-object"],
    )
    def test_invalid_private_settings_fail_before_any_mutation(
        self, monkeypatch, tmp_path, contents, message
    ):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        private_path.write_text(contents, encoding="utf-8")
        managed_path.write_text('{"companyPolicy": "keep"}', encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "managed_configs": {"claude": {"keys": [["env", "ANTHROPIC_BASE_URL"]]}},
        }

        with pytest.raises(RuntimeError, match=message):
            claude.write_tool_config(state, "system.ai.claude-opus-4-8")

        assert private_path.read_text() == contents
        assert json.loads(managed_path.read_text()) == {"companyPolicy": "keep"}
        assert not backup_path.exists()
        assert not metadata_path.exists()

    def test_private_revert_restores_latest_acquisition_baseline(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        picker_one = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        picker_two = {
            "availableModels": ["enterprise-two"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-two"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(picker_one), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        static_state = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }
        claude.write_tool_config(static_state, static_models[0])
        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.models",
        )
        current = json.loads(private_path.read_text())
        current.update(picker_two)
        private_path.write_text(json.dumps(current), encoding="utf-8")
        claude.write_tool_config(static_state, static_models[0])

        assert claude.revert_private_settings(static_state) is True

        restored = json.loads(private_path.read_text())
        assert {key: restored[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker_two

    def test_private_revert_preserves_whole_drifted_picker_group(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        state = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }
        claude.write_tool_config(state, static_models[0])
        drift = {
            "availableModels": ["enterprise-drift"],
            "enforceAvailableModels": True,
            "modelPicker": {
                "replaceBuiltInOptions": False,
                "options": [{"model": "enterprise-drift", "label": "Enterprise"}],
            },
        }
        current = json.loads(private_path.read_text())
        current.update(drift)
        private_path.write_text(json.dumps(current), encoding="utf-8")

        assert claude.revert_private_settings(state) is True

        restored = json.loads(private_path.read_text())
        assert {key: restored[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == drift

    def test_private_revert_retry_after_lease_clear_failure_keeps_restored_target(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        save_lease = claude._save_private_picker_management
        failed = False

        def fail_first_clear(entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected private lease clear failure")
            save_lease(entries)

        monkeypatch.setattr(claude, "_save_private_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected private lease clear failure"):
            claude.revert_private_settings(state)

        assert json.loads(private_path.read_text()) == baseline
        assert not backup_path.exists()
        assert (
            json.loads(metadata_path.read_text())["leases"]["private"]["pending"]["phase"]
            == "revert_written"
        )

        assert claude.revert_private_settings(state) is True
        assert json.loads(private_path.read_text()) == baseline
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"managed"}

    def test_private_revert_without_picker_lease_is_journaled_for_retry(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original = {"companyPolicy": "original"}
        private_path.write_text(json.dumps(original), encoding="utf-8")
        managed_path.write_text("{}", encoding="utf-8")
        state = {"workspace": "https://workspace.example.com", "codex_models": []}
        claude.write_tool_config(state, "system.ai.claude-opus-4-8")
        assert backup_path.exists()
        save_lease = claude._save_private_picker_management
        failed = False

        def fail_first_clear(entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected private lease clear failure")
            save_lease(entries)

        monkeypatch.setattr(claude, "_save_private_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected private lease clear failure"):
            claude.revert_private_settings(state)

        assert json.loads(private_path.read_text()) == original
        assert not backup_path.exists()
        pending = json.loads(metadata_path.read_text())["leases"]["private"]["pending"]
        assert pending["phase"] == "revert_written"
        assert "target_document_sha256" in pending

        externally_edited = json.loads(private_path.read_text())
        externally_edited["external"] = "preserve"
        private_path.write_text(json.dumps(externally_edited), encoding="utf-8")

        assert claude.revert_private_settings(state) is True
        assert json.loads(private_path.read_text()) == {
            **original,
            "external": "preserve",
        }
        assert not metadata_path.exists()

    def test_reconfigure_after_failed_revert_reacquires_complete_private_baseline(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original = {
            "companyPolicy": {"preserve": True},
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(original), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        save_lease = claude._save_private_picker_management
        failed = False

        def fail_first_clear(entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected private lease clear failure")
            save_lease(entries)

        monkeypatch.setattr(claude, "_save_private_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected private lease clear failure"):
            claude.revert_private_settings(state)
        assert json.loads(private_path.read_text()) == original
        assert not backup_path.exists()

        claude.write_tool_config(state, state["claude_static_models"][0])
        assert backup_path.exists()
        assert claude.revert_private_settings(state) is True
        assert json.loads(private_path.read_text()) == original

    def test_reconfigure_replaces_stale_backup_after_verified_revert_drift(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original = {
            "companyPolicy": {"preserve": True},
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(original), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        remove_backup = claude._remove_private_backup
        failed = False

        def fail_first_backup_cleanup():
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("injected crash before backup cleanup")
            remove_backup()

        monkeypatch.setattr(claude, "_remove_private_backup", fail_first_backup_cleanup)
        with pytest.raises(RuntimeError, match="injected crash before backup cleanup"):
            claude.revert_private_settings(state)
        assert json.loads(private_path.read_text()) == original
        assert json.loads(backup_path.read_text()) == original

        drifted = {**original, "external": "preserve this edit"}
        private_path.write_text(json.dumps(drifted), encoding="utf-8")
        claude.write_tool_config(state, state["claude_static_models"][0])
        assert json.loads(backup_path.read_text()) == drifted

        assert claude.revert_private_settings(state) is True
        assert json.loads(private_path.read_text()) == drifted

    def test_verified_revert_exact_before_recreation_is_postwrite_drift(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original = {
            "companyPolicy": {"preserve": True},
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(original), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        exact_before = json.loads(private_path.read_text())
        save_lease = claude._save_private_picker_management
        failed = False

        def fail_first_clear(entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected private lease clear failure")
            save_lease(entries)

        monkeypatch.setattr(claude, "_save_private_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected private lease clear failure"):
            claude.revert_private_settings(state)

        private_path.write_text(json.dumps(exact_before), encoding="utf-8")
        assert claude.revert_private_settings(state) is True
        assert json.loads(private_path.read_text()) == exact_before

    def test_prewrite_revert_drift_does_not_succeed_with_ucode_settings(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        managed_path.write_text("{}", encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        assert not backup_path.exists()
        begin_transition = claude._begin_picker_transition

        def crash_after_journal(*args, **kwargs):
            begin_transition(*args, **kwargs)
            if kwargs.get("phase") == "reverting":
                raise RuntimeError("injected crash after revert journal")

        monkeypatch.setattr(claude, "_begin_picker_transition", crash_after_journal)
        with pytest.raises(RuntimeError, match="injected crash after revert journal"):
            claude.revert_private_settings(state)
        settings = json.loads(private_path.read_text())
        settings["external"] = "edited"
        private_path.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(claude, "_begin_picker_transition", begin_transition)

        with pytest.raises(RuntimeError, match="before its target was verified"):
            claude.revert_private_settings(state)

        remaining = json.loads(private_path.read_text())
        assert remaining["external"] == "edited"
        assert "apiKeyHelper" in remaining
        assert not backup_path.exists()

    def test_reconfigure_does_not_capture_unverified_prewrite_revert_drift(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        managed_path.write_text("{}", encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        begin_transition = claude._begin_picker_transition

        def crash_after_journal(*args, **kwargs):
            begin_transition(*args, **kwargs)
            if kwargs.get("phase") == "reverting":
                raise RuntimeError("injected crash after revert journal")

        monkeypatch.setattr(claude, "_begin_picker_transition", crash_after_journal)
        with pytest.raises(RuntimeError, match="injected crash after revert journal"):
            claude.revert_private_settings(state)
        settings = json.loads(private_path.read_text())
        settings["external"] = "edited"
        private_path.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(claude, "_begin_picker_transition", begin_transition)

        with pytest.raises(RuntimeError, match="unverified external changes"):
            claude.write_tool_config(state, state["claude_static_models"][0])

        assert not backup_path.exists()
        assert json.loads(private_path.read_text())["apiKeyHelper"] == settings["apiKeyHelper"]

    def test_managed_revert_retry_after_lease_clear_failure_clears_stale_lease(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        save_lease = claude._save_managed_picker_management
        failed = False

        def fail_first_clear(path, entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected managed lease clear failure")
            save_lease(path, entries)

        monkeypatch.setattr(claude, "_save_managed_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected managed lease clear failure"):
            claude.revert_managed_settings()

        assert {
            key: json.loads(managed_path.read_text())[key]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        } == baseline
        assert (
            "claude"
            not in json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())["files"]
        )

        assert claude.revert_managed_settings() == "unchanged"
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"private"}

    def test_managed_verified_revert_preserves_recreated_before_picker(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "companyPolicy": "preserve",
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        applied = json.loads(managed_path.read_text())
        applied_picker = {key: applied[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS}
        save_lease = claude._save_managed_picker_management
        failed = False

        def fail_first_clear(path, entries):
            nonlocal failed
            if not entries and not failed:
                failed = True
                raise RuntimeError("injected managed lease clear failure")
            save_lease(path, entries)

        monkeypatch.setattr(claude, "_save_managed_picker_management", fail_first_clear)
        with pytest.raises(RuntimeError, match="injected managed lease clear failure"):
            claude.revert_managed_settings()
        assert (
            json.loads(metadata_path.read_text())["leases"]["managed"]["pending"]["phase"]
            == "revert_written"
        )

        recreated = json.loads(managed_path.read_text())
        recreated.update(applied_picker)
        managed_path.write_text(json.dumps(recreated), encoding="utf-8")

        assert claude.revert_managed_settings() == "unchanged"
        written = json.loads(managed_path.read_text())
        assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == applied_picker
        assert written["companyPolicy"] == "preserve"
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"private"}

    def test_managed_marker_failure_retains_manifest_for_exact_before_retry(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "companyPolicy": "preserve",
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state = {
            "workspace": "https://workspace.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        claude.write_tool_config(state, state["claude_static_models"][0])
        applied = json.loads(managed_path.read_text())
        applied_picker = {key: applied[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS}
        mark_written = claude._mark_picker_revert_written
        failed = False

        def fail_first_managed_marker(scope, path):
            nonlocal failed
            if scope == "managed" and not failed:
                failed = True
                raise RuntimeError("injected managed marker failure")
            mark_written(scope, path)

        monkeypatch.setattr(claude, "_mark_picker_revert_written", fail_first_managed_marker)
        with pytest.raises(RuntimeError, match="injected managed marker failure"):
            claude.revert_managed_settings()

        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        assert "claude" in manifest["files"]
        assert (
            json.loads(metadata_path.read_text())["leases"]["managed"]["pending"]["phase"]
            == "reverting"
        )
        recreated = json.loads(managed_path.read_text())
        recreated.update(applied_picker)
        managed_path.write_text(json.dumps(recreated), encoding="utf-8")

        assert (
            claude.revert_managed_settings() == "ucode entries removed; external changes preserved"
        )
        written = json.loads(managed_path.read_text())
        assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == {
            key: baseline[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        }
        assert written["companyPolicy"] == "preserve"
        assert (
            "claude"
            not in json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())["files"]
        )
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"private"}

    @pytest.mark.parametrize(
        "source_kwargs",
        [
            {"provider": "main.default.anthropic"},
            {"parent_schema": "main.managed_models"},
        ],
        ids=["provider", "model-location"],
    )
    def test_fresh_workspace_restores_preexisting_picker(
        self, monkeypatch, tmp_path, source_kwargs
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original_picker = {
            "availableModels": ["user-model"],
            "enforceAvailableModels": False,
            "modelPicker": {
                "replaceBuiltInOptions": False,
                "options": [{"model": "user-model", "label": "User"}],
            },
        }
        original = {**original_picker, "companyPolicy": {"keep": True}}
        private_path.write_text(json.dumps(original), encoding="utf-8")
        managed_path.write_text(json.dumps(original), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        workspace_a = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }

        claude.write_tool_config(workspace_a, static_models[0])
        assert json.loads(private_path.read_text())["availableModels"] == static_models
        assert json.loads(managed_path.read_text())["availableModels"] == static_models

        workspace_b = {"workspace": "https://workspace-b.example.com", "codex_models": []}
        claude.write_tool_config(workspace_b, None, **source_kwargs)

        for path in (private_path, managed_path):
            written = json.loads(path.read_text())
            assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == (
                original_picker
            )
            assert written["companyPolicy"] == {"keep": True}
        assert not metadata_path.exists()
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        owned_paths = manifest["files"]["claude"]["owned_paths"]
        assert not any([key] in owned_paths for key in claude.CLAUDE_MANAGED_PICKER_KEYS)

    def test_reacquisition_restores_each_scopes_new_picker_baseline(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        picker_one = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps({**picker_one, "companyPolicy": "keep"}), encoding="utf-8")

        static_models = ["system.ai.claude-opus-4-8"]
        static_state = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }
        scoped_state = {"workspace": "https://workspace-b.example.com", "codex_models": []}
        claude.write_tool_config(dict(static_state), static_models[0])
        metadata = json.loads(metadata_path.read_text())
        assert set(metadata["leases"]) == {"private", "managed"}
        for lease in metadata["leases"].values():
            assert set(lease["keys"]) == set(claude.CLAUDE_MANAGED_PICKER_KEYS)
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        assert not any(
            [key] in manifest["files"]["claude"]["owned_paths"]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        )
        claude.write_tool_config(dict(scoped_state), None, parent_schema="main.models")

        picker_two = {
            "availableModels": ["enterprise-two"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-two"}]},
        }
        for path in (private_path, managed_path):
            settings = json.loads(path.read_text())
            settings.update(picker_two)
            path.write_text(json.dumps(settings), encoding="utf-8")

        claude.write_tool_config(dict(static_state), static_models[0])
        claude.write_tool_config(dict(scoped_state), None, provider="main.default.anthropic")

        for path in (private_path, managed_path):
            settings = json.loads(path.read_text())
            assert {key: settings[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker_two
            assert settings["companyPolicy"] == "keep"
        assert not metadata_path.exists()

    def test_private_static_update_failure_keeps_committed_baseline(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state_a = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        state_b = {
            "workspace": "https://workspace-b.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-sonnet-4-6"],
        }
        claude.write_tool_config(dict(state_a), state_a["claude_static_models"][0])
        write_json = claude.write_json_file

        def fail_private_write(path, payload):
            if path == private_path:
                raise RuntimeError("injected private write failure")
            write_json(path, payload)

        monkeypatch.setattr(claude, "write_json_file", fail_private_write)
        with pytest.raises(RuntimeError, match="injected private write failure"):
            claude.write_tool_config(dict(state_b), state_b["claude_static_models"][0])
        assert (
            json.loads(private_path.read_text())["availableModels"]
            == state_a["claude_static_models"]
        )

        monkeypatch.setattr(claude, "write_json_file", write_json)
        claude.write_tool_config(dict(state_b), state_b["claude_static_models"][0])
        claude.write_tool_config(
            {"workspace": "https://workspace-c.example.com", "codex_models": []},
            None,
            parent_schema="main.models",
        )
        settings = json.loads(private_path.read_text())
        assert {key: settings[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == baseline

    def test_managed_static_update_failure_keeps_committed_baseline(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        state_a = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-opus-4-8"],
        }
        state_b = {
            "workspace": "https://workspace-b.example.com",
            "codex_models": [],
            "claude_static_models": ["system.ai.claude-sonnet-4-6"],
        }
        claude.write_tool_config(dict(state_a), state_a["claude_static_models"][0])
        replace = managed_files._sudo_replace
        monkeypatch.setattr(
            managed_files,
            "_sudo_replace",
            lambda path, text: (_ for _ in ()).throw(PermissionError("injected managed failure")),
        )
        with pytest.raises(managed_files.ManagedFileWriteUnavailable):
            claude.write_tool_config(dict(state_b), state_b["claude_static_models"][0])
        assert (
            json.loads(managed_path.read_text())["availableModels"]
            == state_a["claude_static_models"]
        )

        monkeypatch.setattr(managed_files, "_sudo_replace", replace)
        claude.write_tool_config(dict(state_b), state_b["claude_static_models"][0])
        claude.write_tool_config(
            {"workspace": "https://workspace-c.example.com", "codex_models": []},
            None,
            parent_schema="main.models",
        )
        settings = json.loads(managed_path.read_text())
        assert {key: settings[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == baseline

    def test_managed_release_repairs_snapshot_before_clearing_pending_lease(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        claude.write_tool_config(
            {
                "workspace": "https://workspace-a.example.com",
                "codex_models": [],
                "claude_static_models": static_models,
            },
            static_models[0],
        )
        scoped_state = {"workspace": "https://workspace-b.example.com", "codex_models": []}
        record_last_applied = managed_files._record_last_applied

        def fail_record(*args, **kwargs):
            raise RuntimeError("injected managed metadata failure")

        monkeypatch.setattr(managed_files, "_record_last_applied", fail_record)
        with pytest.raises(RuntimeError, match="injected managed metadata failure"):
            claude.write_tool_config(dict(scoped_state), None, parent_schema="main.models")
        assert "pending" in json.loads(metadata_path.read_text())["leases"]["managed"]
        assert {
            key: json.loads(managed_path.read_text())[key]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        } == baseline

        monkeypatch.setattr(managed_files, "_record_last_applied", record_last_applied)
        claude.write_tool_config(dict(scoped_state), None, parent_schema="main.models")

        assert not metadata_path.exists()
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        entry = manifest["files"]["claude"]
        assert not any([key] in entry["owned_paths"] for key in claude.CLAUDE_MANAGED_PICKER_KEYS)
        assert (managed_files.MANAGED_BACKUP_DIR / entry["last_applied_file"]).read_text() == (
            managed_path.read_text()
        )

    def test_managed_revert_uses_latest_acquisition_baseline(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        picker_one = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(picker_one), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        static_state = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }
        scoped_state = {"workspace": "https://workspace-b.example.com", "codex_models": []}
        claude.write_tool_config(dict(static_state), static_models[0])
        claude.write_tool_config(dict(scoped_state), None, parent_schema="main.models")

        picker_two = {
            "availableModels": ["enterprise-two"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-two"}]},
        }
        managed = json.loads(managed_path.read_text())
        managed.update(picker_two)
        managed_path.write_text(json.dumps(managed), encoding="utf-8")
        claude.write_tool_config(dict(static_state), static_models[0])

        assert claude.revert_managed_settings() == "restored"
        restored = json.loads(managed_path.read_text())
        assert {key: restored[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == picker_two
        metadata = json.loads(metadata_path.read_text())
        assert set(metadata["leases"]) == {"private"}

    def test_managed_revert_preserves_whole_drifted_picker_group(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        baseline = {
            "availableModels": ["enterprise-one"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "enterprise-one"}]},
        }
        for path in (private_path, managed_path):
            path.write_text(json.dumps(baseline), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        claude.write_tool_config(
            {
                "workspace": "https://workspace-a.example.com",
                "codex_models": [],
                "claude_static_models": static_models,
            },
            static_models[0],
        )
        drift = {
            "availableModels": ["enterprise-drift"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "enterprise-drift"}]},
        }
        managed = json.loads(managed_path.read_text())
        managed.update(drift)
        managed_path.write_text(json.dumps(managed), encoding="utf-8")

        assert (
            claude.revert_managed_settings() == "ucode entries removed; external changes preserved"
        )
        restored = json.loads(managed_path.read_text())
        assert {key: restored[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == drift
        metadata = json.loads(metadata_path.read_text())
        assert set(metadata["leases"]) == {"private"}

    def test_fresh_workspace_migrates_legacy_picker_ownership(self, monkeypatch, tmp_path):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        original_picker = {
            "availableModels": ["user-model"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "user-model"}]},
        }
        stale_picker = {
            "availableModels": ["system.ai.claude-opus-4-8"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "system.ai.claude-opus-4-8"}]},
        }
        managed_path.write_text(json.dumps(original_picker), encoding="utf-8")
        managed_files.reconcile_managed_file(
            managed_path,
            json.dumps(stale_picker),
            tool="claude",
            display="Claude Code",
            owned_paths=[[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS],
        )
        private_path.write_text(json.dumps(stale_picker), encoding="utf-8")
        backup_path.write_text(json.dumps(original_picker), encoding="utf-8")
        monkeypatch.setattr(
            claude,
            "load_full_state",
            lambda: {
                "workspaces": {
                    "https://workspace-a.example.com": {
                        "managed_configs": {
                            "claude": {"keys": [[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS]}
                        }
                    }
                }
            },
        )

        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.managed_models",
        )

        written = json.loads(private_path.read_text())
        assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == original_picker
        assert json.loads(backup_path.read_text()) == original_picker
        assert not metadata_path.exists()

    def test_legacy_migration_preserves_private_picker_changed_after_ucode(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        stale_picker = {
            "availableModels": ["system.ai.claude-opus-4-8"],
            "enforceAvailableModels": True,
            "modelPicker": {"options": [{"model": "system.ai.claude-opus-4-8"}]},
        }
        managed_path.write_text("{}", encoding="utf-8")
        managed_files.reconcile_managed_file(
            managed_path,
            json.dumps(stale_picker),
            tool="claude",
            display="Claude Code",
            owned_paths=[[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS],
        )
        edited_picker = {
            "availableModels": ["user-edited-model"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "user-edited-model"}]},
        }
        private_path.write_text(json.dumps(edited_picker), encoding="utf-8")
        backup_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            claude,
            "load_full_state",
            lambda: {
                "workspaces": {
                    "https://workspace-a.example.com": {
                        "managed_configs": {
                            "claude": {"keys": [[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS]}
                        }
                    }
                }
            },
        )

        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.managed_models",
        )

        written_private = json.loads(private_path.read_text())
        assert {
            key: written_private[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        } == edited_picker
        assert (
            not set(claude.CLAUDE_MANAGED_PICKER_KEYS) & json.loads(managed_path.read_text()).keys()
        )
        assert not metadata_path.exists()

    def test_fresh_workspace_preserves_post_ucode_picker_edits(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        private_path.write_text('{"companyPolicy": {"keep": true}}', encoding="utf-8")
        managed_path.write_text('{"companyPolicy": {"keep": true}}', encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        claude.write_tool_config(
            {
                "workspace": "https://workspace-a.example.com",
                "codex_models": [],
                "claude_static_models": static_models,
            },
            static_models[0],
        )
        edited_picker = {
            "availableModels": ["enterprise-model"],
            "enforceAvailableModels": True,
            "modelPicker": {
                "replaceBuiltInOptions": True,
                "options": [{"model": "enterprise-model", "label": "Enterprise"}],
            },
        }
        for path in (private_path, managed_path):
            settings = json.loads(path.read_text())
            settings.update(edited_picker)
            path.write_text(json.dumps(settings), encoding="utf-8")

        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.managed_models",
        )
        claude.write_tool_config(
            {"workspace": "https://workspace-c.example.com", "codex_models": []},
            None,
            provider="main.default.anthropic",
        )

        for path in (private_path, managed_path):
            written = json.loads(path.read_text())
            assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == edited_picker
            assert written["companyPolicy"] == {"keep": True}
        assert not metadata_path.exists()
        manifest = json.loads(managed_files.MANAGED_BACKUP_MANIFEST_PATH.read_text())
        owned_paths = manifest["files"]["claude"]["owned_paths"]
        assert not any([key] in owned_paths for key in claude.CLAUDE_MANAGED_PICKER_KEYS)

    def test_required_managed_picker_cleanup_cannot_fall_back_noninteractively(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, _metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        static_models = ["system.ai.claude-opus-4-8"]
        claude.write_tool_config(
            {
                "workspace": "https://workspace-a.example.com",
                "codex_models": [],
                "claude_static_models": static_models,
            },
            static_models[0],
        )
        monkeypatch.setattr(claude, "managed_writes_allowed", lambda: False)

        with pytest.raises(RuntimeError, match="cannot be applied non-interactively"):
            claude.write_tool_config(
                {"workspace": "https://workspace-b.example.com", "codex_models": []},
                None,
                parent_schema="main.managed_models",
            )

        assert "availableModels" not in json.loads(private_path.read_text())
        assert json.loads(managed_path.read_text())["availableModels"] == static_models

    def test_static_picker_sidecar_failure_is_retry_safe(self, monkeypatch, tmp_path):
        private_path, _managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        user_picker = {
            "availableModels": ["user-model"],
            "enforceAvailableModels": False,
            "modelPicker": {"options": [{"model": "user-model"}]},
        }
        private_path.write_text(json.dumps(user_picker), encoding="utf-8")
        static_models = ["system.ai.claude-opus-4-8"]
        static_state = {
            "workspace": "https://workspace-a.example.com",
            "codex_models": [],
            "claude_static_models": static_models,
        }
        begin_picker_transition = claude._begin_picker_transition
        attempts = 0

        def fail_first_sidecar_write(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected sidecar failure")
            begin_picker_transition(*args, **kwargs)

        monkeypatch.setattr(claude, "_begin_picker_transition", fail_first_sidecar_write)

        with pytest.raises(RuntimeError, match="injected sidecar failure"):
            claude.write_tool_config(dict(static_state), static_models[0])
        assert {
            key: json.loads(private_path.read_text())[key]
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        } == user_picker

        claude.write_tool_config(dict(static_state), static_models[0])
        assert json.loads(private_path.read_text())["availableModels"] == static_models
        assert metadata_path.exists()

        claude.write_tool_config(
            {"workspace": "https://workspace-b.example.com", "codex_models": []},
            None,
            parent_schema="main.managed_models",
        )
        written = json.loads(private_path.read_text())
        assert {key: written[key] for key in claude.CLAUDE_MANAGED_PICKER_KEYS} == user_picker
        assert not metadata_path.exists()

    def test_picker_sidecar_supports_each_scope_and_both(self, monkeypatch, tmp_path):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        entries = {
            key: {"original_exists": False, "last_applied": f"applied-{key}"}
            for key in claude.CLAUDE_MANAGED_PICKER_KEYS
        }

        claude._save_private_picker_management(entries)
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"private"}
        claude._save_managed_picker_management(managed_path, entries)
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"private", "managed"}
        claude._save_private_picker_management({})
        assert set(json.loads(metadata_path.read_text())["leases"]) == {"managed"}
        claude._save_managed_picker_management(managed_path, {})
        assert not metadata_path.exists()

        metadata_path.write_text(
            json.dumps({"version": claude.CLAUDE_PICKER_MANAGEMENT_VERSION, "leases": {}}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="Invalid Claude picker metadata"):
            claude._load_picker_management()

    def test_malformed_private_picker_metadata_fails_without_changing_settings(
        self, monkeypatch, tmp_path
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        private_path.write_text('{"user": "keep"}', encoding="utf-8")
        managed_path.write_text('{"companyPolicy": "keep"}', encoding="utf-8")
        metadata_path.write_text("{", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Cannot parse Claude picker metadata"):
            claude.write_tool_config(
                {"workspace": "https://workspace-b.example.com", "codex_models": []},
                None,
                parent_schema="main.managed_models",
            )

        assert json.loads(private_path.read_text()) == {"user": "keep"}
        assert json.loads(managed_path.read_text()) == {"companyPolicy": "keep"}

    @pytest.mark.parametrize("managed_keys", [[], ["availableModels"]], ids=["empty", "partial"])
    def test_incomplete_private_picker_metadata_fails_without_changing_settings(
        self, monkeypatch, tmp_path, managed_keys
    ):
        private_path, managed_path, _backup_path, metadata_path = self._patch_files(
            monkeypatch, tmp_path
        )
        private_path.write_text('{"user": "keep"}', encoding="utf-8")
        managed_path.write_text('{"companyPolicy": "keep"}', encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "version": claude.CLAUDE_PICKER_MANAGEMENT_VERSION,
                    "leases": {
                        "private": {
                            "path": str(private_path),
                            "keys": {
                                key: {"original_exists": False, "last_applied": []}
                                for key in managed_keys
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="Invalid Claude picker metadata"):
            claude.write_tool_config(
                {"workspace": "https://workspace-b.example.com", "codex_models": []},
                None,
                parent_schema="main.managed_models",
            )

        assert json.loads(private_path.read_text()) == {"user": "keep"}
        assert json.loads(managed_path.read_text()) == {"companyPolicy": "keep"}


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
        _patch_private_json_store(monkeypatch, {})
        saved: list[dict] = []
        monkeypatch.setattr(claude, "save_state", lambda state: saved.append(state))
        monkeypatch.setattr(
            claude,
            "load_state",
            lambda: json.loads(json.dumps(saved[-1])) if saved else {},
        )
        monkeypatch.setattr(claude, "remove_claude_mcp_server", lambda name, scope: False)

        attempts = 0

        def boom(name, entry, scope=claude.MCP_USER_SCOPE):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Failed to add MCP server 'web_search' via claude CLI.")

        monkeypatch.setattr(claude, "add_claude_mcp_server", boom)

        state = {"workspace": WS, "codex_models": ["databricks-gpt-5"]}
        result = claude.write_tool_config(state, "databricks-claude-sonnet-4")
        result = claude.write_tool_config(result, "databricks-claude-sonnet-4")
        assert saved, "save_state should still be called when MCP registration fails"
        assert result["workspace"] == WS
        assert attempts == 2


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

            def serve_forever(self):
                calls.append(("serve",))

            def shutdown(self):
                calls.append(("shutdown",))

        class Cache:
            def stop(self):
                calls.append(("stop",))

        class Client:
            def close(self):
                calls.append(("close",))

        class Process:
            def __init__(self, argv):
                calls.append(("popen", argv))

            def wait(self):
                return 0

        def start_proxy(workspace, profile, port, token_header, force_refresh_near_expiry):
            calls.append(
                (
                    "proxy",
                    workspace,
                    profile,
                    port,
                    token_header,
                    force_refresh_near_expiry,
                )
            )
            return Server(), Cache(), Client()

        monkeypatch.setattr(claude, "_managed_relayed_conflicts", lambda: None)
        monkeypatch.setattr(claude, "_ensure_subscription_login", lambda: None)
        monkeypatch.setattr(claude.gateway_proxy, "start_proxy", start_proxy)
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
            "test",
            12345,
            claude.gateway_proxy.AI_GATEWAY_TOKEN_HEADER,
            False,
        )
        assert calls[-3:] == [("stop",), ("shutdown",), ("close",)]

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
        written: dict = {}

        def fake_write(path, payload):
            written["payload"] = payload

        _patch_private_json_store(monkeypatch, existing_settings, fake_write)
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
        _patch_private_json_store(
            monkeypatch, existing, lambda _path, payload: written.append(payload)
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


def test_write_tool_config_serializes_both_scopes_and_state_save(monkeypatch):
    calls: list[str] = []
    calls_lock = threading.Lock()
    b_started = threading.Event()
    errors: list[BaseException] = []

    def record(label: str) -> None:
        with calls_lock:
            calls.append(label)

    def private(state, _overlay, _compose):
        label = state["workspace"]
        record(f"{label}-private")
        if label == "A":
            assert b_started.wait(timeout=5)

    def managed(state, *_args, **_kwargs):
        record(f"{state['workspace']}-managed")

    monkeypatch.setattr(claude, "_reconcile_private_settings", private)
    monkeypatch.setattr(claude, "_reconcile_managed_settings", managed)
    monkeypatch.setattr(claude, "save_state", lambda state: record(f"{state['workspace']}-save"))

    def configure(label: str) -> None:
        try:
            if label == "B":
                b_started.set()
            claude.write_tool_config(
                {"workspace": label, "codex_models": []},
                "system.ai.claude-opus-4-8",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(target=configure, args=("A",))
    thread_b = threading.Thread(target=configure, args=("B",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    assert calls == [
        "A-private",
        "A-managed",
        "A-save",
        "B-private",
        "B-managed",
        "B-save",
    ]


def test_web_search_registration_converges_to_latest_generation(monkeypatch):
    persisted: dict = {}
    persisted_lock = threading.Lock()
    b_saved = threading.Event()
    release_a = threading.Event()
    picker_released = threading.Event()
    registrations: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    monkeypatch.setattr(claude, "_reconcile_private_settings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude, "_reconcile_managed_settings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude, "_web_search_mcp_is_current", lambda *_args: False)

    def save(state):
        with persisted_lock:
            persisted.clear()
            persisted.update(json.loads(json.dumps(state)))
        if state.get("web_search_model") == "model-b":
            b_saved.set()

    def load():
        with persisted_lock:
            return json.loads(json.dumps(persisted))

    def register(_workspace, model, profile=None):
        registrations.append((model, profile))
        if model == "model-a":

            def probe_lock():
                with claude._picker_management_lock():
                    picker_released.set()

            probe = threading.Thread(target=probe_lock)
            probe.start()
            assert picker_released.wait(timeout=5)
            probe.join(timeout=5)
            assert release_a.wait(timeout=5)
        return True

    monkeypatch.setattr(claude, "save_state", save)
    monkeypatch.setattr(claude, "load_state", load)
    monkeypatch.setattr(claude, "_register_web_search_mcp", register)

    def configure(model: str, profile: str) -> None:
        try:
            claude.write_tool_config(
                {
                    "workspace": WS,
                    "profile": profile,
                    "web_search_model": model,
                    "codex_models": [f"fallback-{model}"],
                },
                "system.ai.claude-opus-4-8",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread_a = threading.Thread(target=configure, args=("model-a", "profile-a"))
    thread_b = threading.Thread(target=configure, args=("model-b", "profile-b"))
    thread_a.start()
    assert picker_released.wait(timeout=5)
    thread_b.start()
    assert b_saved.wait(timeout=5)
    release_a.set()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    assert registrations == [("model-a", "profile-a"), ("model-b", "profile-b")]
    latest = load()
    assert latest["web_search_model"] == "model-b"
    assert latest["profile"] == "profile-b"
    assert latest[claude.WEB_SEARCH_MCP_STATE_KEY] == claude._web_search_mcp_entry(
        WS, "model-b", "profile-b"
    )


def test_managed_web_search_registration_uses_persisted_generation(monkeypatch):
    persisted: dict = {}
    registrations: list[tuple[str, str | None]] = []

    monkeypatch.setattr(claude, "_reconcile_private_settings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude, "_reconcile_managed_settings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(claude, "_web_search_mcp_is_current", lambda *_args: False)

    def save(state):
        persisted.clear()
        persisted.update(json.loads(json.dumps(developer_state_from_resolved(state))))

    monkeypatch.setattr(claude, "save_state", save)
    monkeypatch.setattr(claude, "load_state", lambda: json.loads(json.dumps(persisted)))
    monkeypatch.setattr(
        claude,
        "_register_web_search_mcp",
        lambda _workspace, model, profile=None: registrations.append((model, profile)) or True,
    )

    claude.write_tool_config(
        {
            "workspace": WS,
            "profile": "managed-profile",
            "web_search_model": "managed-model",
            "codex_models": ["managed-fallback"],
            MANAGED_OVERLAY_KEY: {
                "profile": "developer-profile",
                "web_search_model": None,
                "codex_models": [],
            },
        },
        "system.ai.claude-opus-4-8",
    )

    expected = claude._web_search_mcp_entry(WS, "managed-model", "managed-profile")
    assert registrations == [("managed-model", "managed-profile")]
    assert persisted[claude.WEB_SEARCH_MCP_STATE_KEY] == expected
    assert persisted[claude.WEB_SEARCH_MCP_GENERATION_KEY]


def test_web_search_registration_rejects_token_preserving_state_change(monkeypatch):
    entry = claude._web_search_mcp_entry(WS, "model-a", "profile-a")
    state = {
        "workspace": WS,
        "profile": "profile-a",
        "web_search_model": "model-a",
        claude.WEB_SEARCH_MCP_GENERATION_KEY: "same-token",
    }
    latest = {**state, "profile": "profile-b"}
    registrations: list[str] = []

    monkeypatch.setattr(claude, "load_state", lambda: dict(latest))
    monkeypatch.setattr(
        claude,
        "_register_web_search_mcp",
        lambda *_args, **_kwargs: registrations.append("registered") or True,
    )

    result = claude._register_web_search_for_current_generation(state, entry)

    assert result == latest
    assert registrations == []
