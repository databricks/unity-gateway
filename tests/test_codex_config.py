from __future__ import annotations

import tomlkit

from ucode.agents import codex
from ucode.codex_config import codex_config_args, strip_hook_trust_state

WS = "https://example.databricks.com"

# The stale trust snapshot Codex leaves in a config layer: per-hook-source
# trusted hashes under hooks.state (AIGTWY-5220).
HOOKS_WITH_STALE_TRUST_STATE = """
[[hooks.PreToolUse]]
matcher = "Agent|.*spawn_agent$"

[hooks.PreToolUse.hooks]
type = "command"
command = "ug codex-router-hook route-subagent"

[hooks.state."/home/u/.codex/hooks.json:post_tool_use:1:0"]
trusted_hash = "sha256:a4ce19f6a4c610a35e632d2bb901bef8b62e0528832f2ab4ee4026b84efa2589"
"""


class TestCodexConfigArgs:
    def test_mcp_servers_override_each_entry_not_the_whole_table(self):
        args = codex_config_args({"mcp_servers": {"a": {"command": "ug"}, "b": {"command": "x"}}})

        assert args[0::2] == ["--config", "--config"]
        parsed = [tomlkit.parse(arg).unwrap() for arg in args[1::2]]
        assert parsed == [
            {"mcp_servers": {"a": {"command": "ug"}}},
            {"mcp_servers": {"b": {"command": "x"}}},
        ]

    def test_layers_provider_overrides_without_replacing_user_config(self, monkeypatch):
        monkeypatch.setattr(codex, "ug_version", lambda: "0.1.0")
        monkeypatch.setattr(codex, "agent_version", lambda binary: "0.148.0")

        overlay = codex.render_overlay(
            WS,
            "gpt-5.6-luna",
            "myprof",
        )
        args = codex_config_args(overlay)

        assert args[:4] == [
            "--config",
            'model_provider="Databricks"',
            "--config",
            'model="gpt-5.6-luna"',
        ]
        provider_override = args[-1]
        assert provider_override.startswith("model_providers.Databricks={")
        assert "/ai-gateway/codex/v1" in provider_override
        assert 'command = "' in provider_override
        assert '"myprof"' in provider_override

    def test_renders_nested_tables_from_parsed_profile(self):
        profile = tomlkit.parse(
            """
model_provider = "ucode-databricks"

[model_providers.ucode-databricks]
name = "Databricks AI Gateway"

[model_providers.ucode-databricks.http_headers]
User-Agent = "ucode"

[model_providers.ucode-databricks.auth]
command = "ucode"
args = ["codex-token"]

[tui.model_availability_nux]
"gpt-5.6-sol" = 1
"""
        )

        args = codex_config_args(profile)

        provider_override = next(
            arg for arg in args if arg.startswith("model_providers.ucode-databricks=")
        )
        assert 'http_headers = {User-Agent = "ucode"}' in provider_override
        assert 'auth = {command = "ucode", args = ["codex-token"]}' in provider_override
        assert 'tui={model_availability_nux = {"gpt-5.6-sol" = 1}}' in args

    def test_never_forwards_hook_trust_state(self):
        profile = tomlkit.parse(f'model_provider = "Databricks"\n{HOOKS_WITH_STALE_TRUST_STATE}')

        args = codex_config_args(profile)

        assert not any(arg.startswith("hooks.state") for arg in args)
        assert not any("trusted_hash" in arg for arg in args)
        # Real hook configuration is still forwarded, entry by entry.
        pre_tool_override = next(arg for arg in args if arg.startswith("hooks.PreToolUse="))
        assert "route-subagent" in pre_tool_override
        assert 'model_provider="Databricks"' in args


class TestStripHookTrustState:
    def test_removes_state_and_keeps_other_hooks(self):
        doc = tomlkit.parse(f"{HOOKS_WITH_STALE_TRUST_STATE}").unwrap()

        assert strip_hook_trust_state(doc) is True

        assert "state" not in doc["hooks"]
        assert doc["hooks"]["PreToolUse"][0]["matcher"] == "Agent|.*spawn_agent$"

    def test_removes_emptied_hooks_table(self):
        doc = {"hooks": {"state": {"/u/hooks.json:pre_tool_use:0:0": {"trusted_hash": "sha256:x"}}}}

        assert strip_hook_trust_state(doc) is True

        assert "hooks" not in doc

    def test_returns_false_without_state(self):
        doc = {"hooks": {"PreToolUse": []}}

        assert strip_hook_trust_state(doc) is False

    def test_returns_false_without_hooks(self):
        doc = {"model_provider": "Databricks"}

        assert strip_hook_trust_state(doc) is False
