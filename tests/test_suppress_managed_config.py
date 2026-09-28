"""Tests for the hidden ``--suppress-managed-config`` launch opt-out (AIGTWY-4886).

An ordinary ``ug claude`` / ``ug codex`` launch fetches the workspace's API managed config and
applies it over the developer's settings. ``--suppress-managed-config`` skips that for the launch,
so the developer's own settings stand; the default (no flag) is unchanged. End-to-end launch
behavior is covered by the integration suite; here we cover the flag's wiring, the source-level
suppression, and the preserved default fetch-and-apply path.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

import ucode.cli as cli
import ucode.databricks as db_mod
import ucode.managed_config as mc_mod
from ucode.managed_config import (
    managed_config_reads_suppressed,
    refresh_managed_config,
    set_managed_config_reads_suppressed,
)
from ucode.managed_resolve import resolve_state
from ucode.state import MANAGED_OVERLAY_KEY

runner = CliRunner()

WORKSPACE = "https://example.databricks.com"

# A minimal published CodingAgentConfig (current wire shape) that sets a Claude header.
PUBLISHED_CONFIG = {
    "spec_version": 1,
    "default_agent": "CODING_AGENT_CLAUDE_CODE",
    "enabled_agents": [
        {
            "agent": "CODING_AGENT_CLAUDE_CODE",
            "config": {
                "default_models": {"default_model": "system.ai.claude-opus-4-8"},
                "http_headers": {"x-managed-header": "managed"},
            },
        },
    ],
}


def test_flag_hidden_and_wired_to_launch_tool():
    """The flag is hidden from help and forwards its value to _launch_tool for both agents."""
    for tool in ("claude", "codex"):
        assert "--suppress-managed-config" not in runner.invoke(cli.app, [tool, "--help"]).output
        with patch("ucode.cli._launch_tool") as launch:
            runner.invoke(cli.app, [tool, "--suppress-managed-config"])
            assert launch.call_args.kwargs["suppress_managed_config"] is True
        with patch("ucode.cli._launch_tool") as launch:
            runner.invoke(cli.app, [tool])
            assert launch.call_args.kwargs["suppress_managed_config"] is False


def test_default_launch_still_applies_published_config(tmp_path, monkeypatch):
    """Without the flag, a published config is fetched and overlaid on the developer's settings.

    This is the behavior --suppress-managed-config must leave intact when the flag is absent.
    """
    stub = tmp_path / "published.json"
    stub.write_text(json.dumps(PUBLISHED_CONFIG), encoding="utf-8")
    monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(stub))
    monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile=None: "tok")
    developer_state = {"workspace": WORKSPACE, "claude_http_headers": {"x-developer": "local"}}

    manifest, feature_disabled = refresh_managed_config(developer_state)

    assert feature_disabled is False
    assert manifest is not None
    resolved = resolve_state(manifest, developer_state, "claude")
    assert resolved["claude_http_headers"] == {"x-managed-header": "managed"}
    assert resolved[MANAGED_OVERLAY_KEY]["claude_http_headers"] == {"x-developer": "local"}


def test_suppress_signal_forces_unmanaged_despite_published_config(tmp_path, monkeypatch):
    """With reads suppressed, the shared source read returns none even though the workspace
    publishes a config, so every downstream settings writer treats the launch as unmanaged."""
    stub = tmp_path / "published.json"
    stub.write_text(json.dumps(PUBLISHED_CONFIG), encoding="utf-8")
    monkeypatch.setenv("UCODE_MANAGED_CONFIG_STUB", str(stub))
    monkeypatch.setattr(mc_mod, "get_databricks_token", lambda ws, profile=None: "tok")
    set_managed_config_reads_suppressed(True)
    try:
        manifest, feature_disabled = refresh_managed_config({"workspace": WORKSPACE})
    finally:
        set_managed_config_reads_suppressed(False)

    assert manifest is None
    assert feature_disabled is False


# A launch state with no configured tools, so a launch takes the auto-configure branch.
_FRESH_LAUNCH_STATE = {
    "workspace": WORKSPACE,
    "base_urls": {
        "claude": f"{WORKSPACE}/ai-gateway/anthropic",
        "codex": f"{WORKSPACE}/ai-gateway/codex",
    },
    "claude_models": {"sonnet": "databricks-claude-sonnet-4"},
    "codex_models": ["codex-mini"],
    "managed_configs": {},
    "available_tools": [],
}


def _invoke_suppressed_claude_launch():
    """Drive a fresh suppressed `ug claude` launch with the heavy launch deps mocked out.

    Returns (result, seen) where seen["suppressed"] records whether reads were suppressed at the
    moment auto-configure ran.
    """
    seen = {}

    def record(tool, **kwargs):
        seen["suppressed"] = managed_config_reads_suppressed()

    state = dict(_FRESH_LAUNCH_STATE)
    with (
        patch("ucode.cli.ensure_bootstrap_dependencies"),
        patch("ucode.cli.load_state", return_value=state),
        patch("ucode.cli._auto_configure_tool", side_effect=record),
        patch("ucode.cli.ensure_provider_state", return_value=state),
        patch("ucode.cli.configure_shared_state", return_value=state),
        patch("ucode.cli.resolve_provider_models", return_value=(None, None, False)),
        patch("ucode.cli.get_databricks_token", return_value="token"),
        patch(
            "ucode.cli.list_anthropic_model_catalog",
            return_value=db_mod.AnthropicModelCatalog(
                model_ids=["claude-sonnet-5"], model_id_to_display_name={}
            ),
        ),
        patch("ucode.cli.configure_tool", return_value=state),
        patch("ucode.cli.launch_agent"),
    ):
        result = runner.invoke(cli.app, ["claude", "--suppress-managed-config"])
    return result, seen


def test_suppression_active_during_auto_configure_and_reset_after():
    """A fresh suppressed launch suppresses reads before auto-configure runs (so auto-configure
    cannot apply a published config) and clears the switch once the launch finishes."""
    result, seen = _invoke_suppressed_claude_launch()

    assert result.exit_code == 0, result.output
    # Set before auto-configure, so auto-configure's own managed read sees none.
    assert seen["suppressed"] is True
    # Restored in _launch_tool's finally, so it never leaks past the launch.
    assert managed_config_reads_suppressed() is False


def test_suppression_restores_prior_value_for_a_nested_launch():
    """When reads are already suppressed (an outer launch), a nested suppressed launch restores the
    prior True rather than clearing it, so the outer launch stays suppressed."""
    set_managed_config_reads_suppressed(True)
    try:
        result, seen = _invoke_suppressed_claude_launch()
        assert result.exit_code == 0, result.output
        assert seen["suppressed"] is True
        # Restored to the prior value (True), not hard-reset to False.
        assert managed_config_reads_suppressed() is True
    finally:
        set_managed_config_reads_suppressed(False)
