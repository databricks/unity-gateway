"""Claude model-discovery CUJs for repository scenarios 7, 9, 11, and 13."""

import json

import pytest
from utils.model_discovery import claude_model_in_picker, claude_system_model_ids
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.claude, pytest.mark.usefixtures("unmanaged_workspace")]


def _assert_scoped_models_in_picker(session, screen, expected_ids):
    models = session.claude_gateway_models()
    assert [model.get("id") for model in models] == expected_ids, models
    display_names = [model.get("display_name") for model in models]
    assert all(isinstance(name, str) and name for name in display_names), models
    for model, display_name in zip(models, display_names, strict=True):
        assert claude_model_in_picker(screen, model["id"], display_name), screen


def _assert_system_models_in_picker(session, screen):
    models = session.claude_gateway_models()
    ids = claude_system_model_ids(models)
    discovered = session.workspace_state()["claude_models"]
    assert discovered, "ug configure found no Claude system.ai models"
    # ug caches the newest system.ai model per family from UC model-services, which can list a
    # just-released model (e.g. a new opus tier) before Claude Code's own gateway-model discovery
    # surfaces it in the picker cache. Require the cached defaults to be well-formed system.ai
    # Claude ids that overlap Claude Code's catalog, rather than a strict subset of a catalog that
    # can legitimately lag behind UC model-services.
    assert all(model_id.startswith("system.ai.claude-") for model_id in discovered.values()), (
        discovered
    )
    assert set(discovered.values()) & set(ids), (discovered, models)
    assert any(
        claude_model_in_picker(screen, model["id"], model.get("display_name")) for model in models
    ), screen


def _assert_replacement_picker(session, expected_ids):
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    assert not {"availableModels", "enforceAvailableModels"} & settings.keys(), settings
    picker = settings["modelPicker"]
    assert picker["replaceBuiltInOptions"] is True, picker
    assert [option["model"] for option in picker["options"]] == expected_ids, picker


@pytest.mark.live
@pytest.mark.tui
def test_case_07_configured_claude_discovers_system_models(live_session, workspace):
    """Scenario: configure Claude, then launch without source overrides or discovery flags.

    Expected: native discovery caches system.ai models as raw IDs or recognized Claude
    gateway aliases and shows a discovered picker entry.
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
    with AgentTerminal(session, "claude", command, "case-07-system-models") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_system_models_in_picker(session, screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_09_fresh_claude_discovers_system_models(live_session, workspace):
    """Scenario: launch fresh Claude with --workspace and no discovery flags.

    Expected: native discovery caches system.ai models as raw IDs or recognized Claude
    gateway aliases and shows a discovered picker entry.
    """
    session = live_session
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-09-system-models") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_system_models_in_picker(session, screen)


@pytest.mark.live
@pytest.mark.tui
def test_case_11_configured_claude_provider_discovers_models_by_default(
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
    with AgentTerminal(session, "claude", command, "case-11-provider-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_provider_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_11_fresh_claude_provider_discovers_models_by_default(
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
    with AgentTerminal(session, "claude", command, "case-11-provider-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_provider_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_13_configured_claude_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: configure Claude, then launch with --model-location.

    Expected: the explicit parent's catalog replaces built-in picker rows and is visible in /model.
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
    with AgentTerminal(session, "claude", command, "case-13-location-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_parent_model])
    _assert_replacement_picker(session, [claude_parent_model])


@pytest.mark.live
@pytest.mark.tui
def test_case_13_fresh_claude_model_location_discovers_parent_models(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: launch fresh Claude with --model-location.

    Expected: the explicit parent's catalog replaces built-in picker rows and is visible in /model.
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
    with AgentTerminal(session, "claude", command, "case-13-location-default") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_scoped_models_in_picker(session, screen, [claude_parent_model])
    _assert_replacement_picker(session, [claude_parent_model])
