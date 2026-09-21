"""Claude CUJs for Tests-table cases 13, 15, 17, 19, 21, and 23."""

import re

import pytest
from utils.terminal import AgentTerminal

pytestmark = pytest.mark.claude

CLAUDE_NATIVE_MODEL_FAMILIES = ("Opus", "Sonnet", "Haiku")


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


def _assert_native_models_in_picker(screen):
    for family in CLAUDE_NATIVE_MODEL_FAMILIES:
        assert family in screen, screen


def _assert_no_claude_owned_gateway_cache_after_launch(session):
    """Check Claude Code's own cache only after its picker process has exited."""
    assert not (session.home / ".claude/cache/gateway-models.json").exists()


@pytest.mark.live
@pytest.mark.tui
def test_case_13_configured_claude_reuses_saved_model_location(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: configure Claude with --model-location, then launch without options.

    Expected: the saved parent supplies Claude's discovered model catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    command = [str(session.binary), "claude"]
    with AgentTerminal(session, "claude", command, "case-13-saved-location") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_parent_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_15_fresh_claude_uses_system_models_when_discovery_disabled(live_session, workspace):
    """Scenario: launch fresh Claude with UG_ENABLE_MODEL_DISCOVERY=0.

    Expected: ug uses its discovered system.ai family models without a gateway catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-15-system-models") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    models = session.workspace_state()["claude_models"]
    assert models
    assert all(model.startswith("system.ai.") for model in models.values())
    for model_id in models.values():
        assert model_id in screen, screen
    _assert_no_claude_owned_gateway_cache_after_launch(session)


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


@pytest.mark.live
@pytest.mark.tui
def test_case_21_configured_claude_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: configure Claude, disable discovery, and launch with --provider.

    Expected: Claude uses native aliases and creates no cache after picker launch.
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
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    command = [str(session.binary), "claude", "--provider", claude_provider]
    with AgentTerminal(session, "claude", command, "case-21-provider-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_no_claude_owned_gateway_cache_after_launch(session)
    _assert_native_models_in_picker(screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_21_fresh_claude_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: disable discovery and launch fresh Claude with --provider.

    Expected: Claude uses native aliases and creates no cache after picker launch.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
    ]
    with AgentTerminal(session, "claude", command, "case-21-provider-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_no_claude_owned_gateway_cache_after_launch(session)
    _assert_native_models_in_picker(screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_23_configured_claude_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: configure Claude, disable discovery, and launch with a parent.

    Expected: Claude uses native aliases and creates no cache after picker launch.
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
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    command = [str(session.binary), "claude", "--model-location", parent_schema]
    with AgentTerminal(session, "claude", command, "case-23-location-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    models = session.workspace_state()["claude_models"]
    assert models
    assert all(model.startswith("system.ai.") for model in models.values())
    _assert_no_claude_owned_gateway_cache_after_launch(session)
    _assert_native_models_in_picker(screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_23_fresh_claude_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: disable discovery and launch fresh Claude with a parent.

    Expected: Claude uses native aliases and creates no cache after picker launch.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
    ]
    with AgentTerminal(session, "claude", command, "case-23-location-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    models = session.workspace_state()["claude_models"]
    assert models
    assert all(model.startswith("system.ai.") for model in models.values())
    _assert_no_claude_owned_gateway_cache_after_launch(session)
    _assert_native_models_in_picker(screen)
