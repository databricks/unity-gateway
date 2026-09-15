"""Red CUJs for every Claude row in the model-discovery Tests table."""

import pytest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.claude]


@pytest.mark.tui
def test_case_01_managed_claude_uses_admin_discovery_after_configure(
    managed_live_session, managed_workspace, managed_claude_model
):
    """Scenario: configure Claude, then launch in a workspace with managed discovery.

    Expected: the managed model catalog wins over the developer's saved setup.
    """
    session = managed_live_session
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        managed_workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    command = [str(session.binary), "claude"]
    with AgentTerminal(session, "claude", command, "case-01-managed") as tui:
        tui.boot()
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [managed_claude_model]


@pytest.mark.tui
def test_case_03_managed_claude_ignores_discovery_disable(
    managed_live_session, managed_workspace, managed_claude_model
):
    """Scenario: launch managed Claude with UG_ENABLE_MODEL_DISCOVERY=0.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    command = [str(session.binary), "claude", "--workspace", managed_workspace]
    with AgentTerminal(session, "claude", command, "case-03-managed-disabled") as tui:
        tui.boot()
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [managed_claude_model]


def test_case_05_managed_claude_rejects_provider_override(
    managed_live_session, managed_workspace, claude_provider
):
    """Scenario: pass --provider when the workspace manages Claude discovery.

    Expected: ug rejects the developer override before Claude starts.
    """
    result = managed_live_session.run(
        "claude",
        "--workspace",
        managed_workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_07_managed_claude_rejects_model_location_override(
    managed_live_session, managed_workspace, parent_schema
):
    """Scenario: pass --model-location when the workspace manages Claude discovery.

    Expected: ug rejects the developer override before Claude starts.
    """
    result = managed_live_session.run(
        "claude",
        "--workspace",
        managed_workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_09_managed_claude_rejects_provider_when_discovery_disabled(
    managed_live_session, managed_workspace, claude_provider
):
    """Scenario: disable discovery and pass --provider in a managed workspace.

    Expected: the managed-config override remains invalid and ug rejects it.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    result = session.run(
        "claude",
        "--workspace",
        managed_workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_11_managed_claude_rejects_model_location_when_discovery_disabled(
    managed_live_session, managed_workspace, parent_schema
):
    """Scenario: disable discovery and pass --model-location in a managed workspace.

    Expected: the managed-config override remains invalid and ug rejects it.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    result = session.run(
        "claude",
        "--workspace",
        managed_workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


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
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [claude_parent_model]


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
        tui.open_model_picker()
        tui.exit_normally()

    models = session.workspace_state()["claude_models"]
    assert models
    assert all(model.startswith("system.ai.") for model in models.values())
    assert not (session.home / ".claude/cache/gateway-models.json").exists()


@pytest.mark.tui
def test_case_17_configured_claude_provider_discovers_models_by_default(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: configure Hosted Claude, then launch with --provider and no opt-in flag.

    Expected: provider model discovery is automatic and replaces the saved catalog.
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
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [claude_provider_model]


@pytest.mark.tui
def test_case_19_configured_claude_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: configure Hosted Claude, then launch with --model-location.

    Expected: the explicit parent overrides the saved Hosted configuration.
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
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [claude_parent_model]


@pytest.mark.tui
def test_case_21_claude_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: configure Claude, disable discovery, then launch with --provider.

    Expected: Claude uses native family aliases and writes no discovered gateway catalog.
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
        tui.open_model_picker()
        tui.exit_normally()

    assert not (session.home / ".claude/cache/gateway-models.json").exists()


@pytest.mark.tui
def test_case_23_claude_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: configure Claude, disable discovery, then pass --model-location.

    Expected: Claude uses native family aliases and writes no discovered gateway catalog.
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
        tui.open_model_picker()
        tui.exit_normally()

    models = session.workspace_state()["claude_models"]
    assert models
    assert all(model.startswith("system.ai.") for model in models.values())
    assert not (session.home / ".claude/cache/gateway-models.json").exists()
