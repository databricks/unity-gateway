"""THROWAWAY diagnostics: confirm or refute the suspected managed-config bugs against real ug.

Each probe asserts the CORRECT behavior; a failure in CI confirms the bug (with the real value in
the message). This file is not shipped, it exists only to make the bug list definite. It asserts
config artifacts on purpose (that is how we witness the apply-layer behavior for confirmation).
"""

import json

import pytest
from utils.managed import (
    build_claude_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)

OPUS = "system.ai.claude-opus-4-8"
SONNET = "system.ai.claude-sonnet-4-6"


def _configure(session, workspace, ok=True):
    return session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240, ok=ok)


def _state_tools(session):
    state = json.loads((session.home / ".ucode" / "state.json").read_text())
    return state["workspaces"][state["current_workspace"]].get("available_tools")


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_probe_family_default_is_honored(live_session, workspace, tmp_path):
    """CORRECT: an admin default_sonnet_model in the allow-list becomes the sonnet default."""
    session = live_session
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        {
            "agent": "CODING_AGENT_CLAUDE_CODE",
            "config": {
                "models": {"model_services": [OPUS, SONNET]},
                "default_models": {"default_model": OPUS, "default_sonnet_model": SONNET},
            },
        },
    )
    set_managed_config_stub(session, tmp_path, config)
    result = _configure(session, workspace)
    assert "managed config is published" in result.stdout, result.stdout
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    # Consumption proof: the injected two-model list drove availableModels (live has three).
    assert settings.get("availableModels") == [OPUS, SONNET], settings
    env = settings.get("env") or {}
    written = env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
    base = written[: -len("[1m]")] if written.endswith("[1m]") else written
    assert base == SONNET, f"sonnet default was {written!r}, expected {SONNET}"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_probe_invalid_default_agent_is_rejected(live_session, workspace, tmp_path):
    """CORRECT: configure rejects a config whose default_agent is not in enabled_agents."""
    session = live_session
    config = build_coding_agent_config("CODING_AGENT_CODEX", build_claude_agent_config([OPUS]))
    set_managed_config_stub(session, tmp_path, config)
    result = _configure(session, workspace, ok=False)
    assert result.returncode != 0, f"configure accepted an invalid config:\n{result.stdout}"


@pytest.mark.managed_fixture
@pytest.mark.claude
def test_probe_deenabling_an_agent_removes_it(live_session, workspace, tmp_path):
    """CORRECT: re-configuring with a claude-only config removes a previously-configured codex."""
    session = live_session
    both = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        build_claude_agent_config([OPUS]),
        {
            "agent": "CODING_AGENT_CODEX",
            "config": {
                "models": {"model_services": ["system.ai.gpt-5-6-sol"]},
                "default_models": {"default_model": "system.ai.gpt-5-6-sol"},
            },
        },
    )
    set_managed_config_stub(session, tmp_path, both)
    _configure(session, workspace)
    assert set(_state_tools(session)) == {"claude", "codex"}, _state_tools(session)

    claude_only = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE", build_claude_agent_config([OPUS])
    )
    set_managed_config_stub(session, tmp_path, claude_only)
    _configure(session, workspace)
    assert _state_tools(session) == ["claude"], _state_tools(session)
