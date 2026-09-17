"""CUJs: configure against a managed workspace, where an admin publishes the setup.

These run against the managed e2e workspace (`E2E_ADMIN_WORKSPACE`), which publishes a
CodingAgentConfig. They are the only journeys that exercise the managed path end to end:
`ug configure` applies the admin config to every enabled agent without the personal agent
selector, and each agent's generated config exposes exactly the admin's static
`model_services` (Claude's `availableModels`/`modelPicker`, Codex's model catalog). The
expected model ids mirror the published config; update them here if the admin list changes.
"""

import json

import pytest
from utils.terminal import AgentTerminal

MANAGED_CLAUDE_MODELS = [
    "system.ai.claude-opus-4-8",
    "system.ai.claude-sonnet-4-6",
    "system.ai.claude-haiku-4-5",
]
MANAGED_CODEX_MODELS = [
    "system.ai.gpt-5-6-sol",
    "system.ai.gpt-99",
]


@pytest.mark.managed
@pytest.mark.claude
def test_ug_configure_managed_claude(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Claude's generated settings expose exactly the admin's static
    model_services as its picker allow-list, and launching Claude reaches a real gateway
    prompt rather than the account-login flow.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "managed config is published" in result.stdout, result.stdout

    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    assert settings.get("availableModels") == MANAGED_CLAUDE_MODELS, settings
    options = (settings.get("modelPicker") or {}).get("options", [])
    assert [option.get("model") for option in options] == MANAGED_CLAUDE_MODELS, settings

    # Drive the real agent: with the managed models in place it must reach a usable
    # gateway prompt, not Claude's own login flow (boot asserts the latter never appears).
    with AgentTerminal(session, "claude", [str(session.binary), "claude"], "managed-claude") as tui:
        tui.boot()
        tui.check_input_and_exit()


@pytest.mark.managed
@pytest.mark.codex
def test_ug_configure_managed_codex(live_session, workspace):
    """Scenario: run `ug configure` on a workspace that publishes a managed config.

    Expected: ug applies the admin config to every enabled agent without showing the
    personal agent selector, Codex's generated model catalog lists exactly the admin's static
    model_services, a model missing bundled metadata uses the generic fallback, and launching
    Codex reaches a real gateway prompt rather than the account-login flow.
    """
    session = live_session
    result = session.run("configure", "--workspace", workspace, "--skip-upgrade", timeout=240)
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    assert "managed config is published" in result.stdout, result.stdout
    assert "Codex is missing metadata for managed GPT model" in result.stdout, result.stdout
    assert "system.ai.gpt-99" in result.stdout, result.stdout
    assert "`ug codex update`" in result.stdout, result.stdout

    catalog = json.loads((session.home / ".ucode" / "codex-model-catalog.json").read_text())
    models = catalog.get("models", [])
    listed = [model.get("slug") for model in models if model.get("visibility") == "list"]
    assert listed == MANAGED_CODEX_MODELS, catalog
    fallback = next(model for model in models if model.get("slug") == "system.ai.gpt-99")
    assert fallback.get("tool_mode") is None, fallback
    assert fallback.get("input_modalities") == ["text"], fallback
    assert fallback.get("context_window") == 32768, fallback
    assert fallback.get("default_reasoning_level") == "none", fallback

    with AgentTerminal(session, "codex", [str(session.binary), "codex"], "managed-codex") as tui:
        tui.boot()
        tui.check_input_and_exit()
