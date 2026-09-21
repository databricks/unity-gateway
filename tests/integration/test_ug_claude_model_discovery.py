"""Claude CUJs for Tests-table cases 13, 15, 17, and 19."""

import re

import pytest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.claude, pytest.mark.usefixtures("unmanaged_workspace")]


def _assert_scoped_models_in_picker(session, screen, expected_ids):
    models = session.claude_gateway_models()
    assert [model.get("id") for model in models] == expected_ids, models
    display_names = [model.get("display_name") for model in models]
    assert all(isinstance(name, str) and name for name in display_names), models
    for model, display_name in zip(models, display_names, strict=True):
        if model["id"] == "claude-haiku-4-5-20251001":
            # Claude deduplicates this built-in model into its native Haiku row,
            # rather than adding the gateway's raw display name as a custom row.
            # Match the actual picker row, not the current-model startup banner.
            assert re.search(r"(?m)^\s*(?:❯\s*)?\d+\.\s+Haiku\b[^\n]*\bHaiku 4\.5\b", screen), (
                screen
            )
        else:
            assert display_name in screen, screen


def _assert_system_models_in_picker(session, screen):
    models = session.claude_gateway_models()
    ids = [model.get("id") for model in models]
    assert ids and all(isinstance(model, str) and model.startswith("system.ai.") for model in ids)
    assert len(ids) == len(set(ids)), models
    discovered = session.workspace_state()["claude_models"]
    assert discovered, "ug configure found no Claude system.ai models"
    assert set(discovered.values()) <= set(ids), (discovered, models)
    assert any(
        model["id"] in screen or (model.get("display_name") and model["display_name"] in screen)
        for model in models
    ), screen


@pytest.mark.live
@pytest.mark.tui
def test_case_13_configured_claude_discovers_system_models(live_session, workspace):
    """Scenario: configure Claude, then launch without source overrides or discovery flags.

    Expected: native discovery caches system.ai models and shows a discovered picker entry.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    command = [str(session.binary), "claude"]
    with AgentTerminal(session, "claude", command, "case-13-system-models") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_system_models_in_picker(session, screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_15_fresh_claude_discovers_system_models(live_session, workspace):
    """Scenario: launch fresh Claude with --workspace and no discovery flags.

    Expected: native discovery caches system.ai models and shows a discovered picker entry.
    """
    session = live_session
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-15-system-models") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_system_models_in_picker(session, screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_17_configured_claude_provider_discovers_models_by_default(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: configure Claude, then launch with --provider and no opt-in flag.

    Expected: the cache contains exactly the provider model and the picker shows
    its row (the native Haiku 4.5 row for the default provider fixture).
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    command = [str(session.binary), "claude", "--provider", claude_provider]
    with AgentTerminal(session, "claude", command, "case-17-provider-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_provider_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_17_fresh_claude_provider_discovers_models_by_default(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: launch fresh Claude with --provider and no opt-in flag.

    Expected: the cache contains exactly the provider model and the picker shows
    its row (the native Haiku 4.5 row for the default provider fixture).
    """
    session = live_session
    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
    ]
    with AgentTerminal(session, "claude", command, "case-17-provider-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_provider_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_19_configured_claude_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: configure Claude, then launch with --model-location.

    Expected: the explicit parent overrides saved setup with its exact picker catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    command = [str(session.binary), "claude", "--model-location", parent_schema]
    with AgentTerminal(session, "claude", command, "case-19-location-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_parent_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_19_fresh_claude_model_location_discovers_parent_models(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: launch fresh Claude with --model-location.

    Expected: the explicit parent supplies its exact picker catalog.
    """
    session = live_session
    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
    ]
    with AgentTerminal(session, "claude", command, "case-19-location-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_parent_model])
