"""Managed-config CUJs for agent model lists and Codex catalog fallback metadata.

The admin CodingAgentConfig is injected via UCODE_MANAGED_CONFIG_STUB so the real /model TUI can be
driven against a model list the live workspace does not publish; only the config INPUT is stubbed
(auth, the config writers, and the agent binary stay real). See tests/AGENTS.md rule 4.
"""

import json

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal

CLAUDE_OPUS = "system.ai.claude-opus-4-8"
# A real ca-central model absent from the live published config: its presence in the picker can
# only come from the injected config, which the live workspace's model list cannot produce.
CLAUDE_OFF_MENU = "system.ai.claude-sonnet-5"
LIVE_ONLY = "haiku-4-5"  # published live, but not in the injected list below
CODEX_DEFAULT = "system.ai.gpt-5-6-sol"
CODEX_WITHOUT_BUNDLED_METADATA = "system.ai.gpt-99"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_managed_fixture_claude_model_picker_reflects_the_config(live_session, workspace, tmp_path):
    """Scenario: launch Claude under an injected managed config and open the /model picker.

    Expected: the picker offers the injected models (including one the live workspace does not
    publish) and omits a model the live workspace does publish.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config([CLAUDE_OPUS, CLAUDE_OFF_MENU])
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-model") as tui:
        tui.boot()
        tui.send("/model", "type the /model command")
        tui.send("\r", "open the model picker")
        tui.wait_for(
            lambda s: "sonnet-5" in s, "the /model picker to list the injected model", timeout=60
        )
        assert LIVE_ONLY not in tui.visible, tui.visible


@pytest.mark.managed_fixture
@pytest.mark.codex
@pytest.mark.parametrize("routing", ["0", "1"], ids=["routing-off", "routing-on"])
def test_ug_configure_managed_codex_catalog_fallback(live_session, workspace, tmp_path, routing):
    """Scenario: configure Codex from an injected model list containing an unknown GPT model.

    Expected: ug creates conservative fallback metadata for the unknown model, warns how to get
    richer metadata, and the real Codex TUI lists that model in its /model picker both with and
    without smart routing.
    """
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(models=[CODEX_DEFAULT, CODEX_WITHOUT_BUNDLED_METADATA]),
    )
    set_managed_config_stub(session, tmp_path, config)
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "Codex is missing metadata for managed GPT model" in result.stdout, result.stdout
    assert CODEX_WITHOUT_BUNDLED_METADATA in result.stdout, result.stdout
    assert "`ug codex update`" in result.stdout, result.stdout

    catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
    models = catalog.get("models", [])
    listed = [model.get("slug") for model in models if model.get("visibility") == "list"]
    assert listed == [CODEX_DEFAULT, CODEX_WITHOUT_BUNDLED_METADATA], catalog
    fallback = next(
        model for model in models if model.get("slug") == CODEX_WITHOUT_BUNDLED_METADATA
    )
    assert fallback.get("tool_mode") is None, fallback
    assert fallback.get("input_modalities") == ["text"], fallback
    assert fallback.get("context_window") == 32768, fallback
    assert fallback.get("default_reasoning_level") == "none", fallback

    session.env["ENABLE_SMART_ROUTING_V2"] = routing
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], f"managed-fallback-routing-{routing}"
    ) as tui:
        tui.boot()
        tui.submit("/model")
        tui.wait_for(
            lambda s: "gpt-99" in s,
            "the /model picker to list the injected custom-catalog model",
            timeout=60,
        )
        tui.send("\x1b", "close the model picker")
        tui.wait_for(lambda s: "Select Model and Effort" not in s, "the model picker to close")
        tui.exit_normally()
