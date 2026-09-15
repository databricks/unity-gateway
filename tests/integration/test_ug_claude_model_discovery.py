"""CUJs: Claude model discovery through explicit parent and provider scopes."""

import pytest
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.claude]


@pytest.mark.tui
def test_ug_claude_fresh_provider_discovers_only_anthropic_mps_models(
    live_session, workspace, claude_provider, claude_provider_model
):
    """Scenario: launch Claude with an Anthropic MPS from a fresh home.

    Expected: Claude's real gateway catalog contains only that MPS's model.
    """
    session = live_session

    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
        "--enable-model-discovery",
    ]
    with AgentTerminal(session, "claude", command, "fresh-provider-models") as tui:
        tui.boot()
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [claude_provider_model]
    session.assert_not_routed()


@pytest.mark.tui
def test_ug_claude_fresh_parent_discovers_only_parent_models(
    live_session, workspace, parent_schema, claude_parent_model
):
    """Scenario: launch Claude with a Unity Catalog parent from a fresh home.

    Expected: Claude's real gateway catalog contains only the compatible Model
    Service in that schema.
    """
    session = live_session

    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--parent",
        parent_schema,
    ]
    with AgentTerminal(session, "claude", command, "fresh-parent-models") as tui:
        tui.boot()
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [claude_parent_model]
    session.assert_not_routed()


@pytest.mark.tui
def test_ug_claude_configured_scope_switch_refreshes_provider_then_parent_models(
    live_session,
    workspace,
    claude_provider,
    claude_provider_model,
    parent_schema,
    claude_parent_model,
):
    """Scenario: configure Hosted Claude, then launch with MPS and parent scopes.

    Expected: each explicit scope overrides the saved setup, and the second
    launch replaces the first launch's cached catalog instead of reusing it.
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

    provider_command = [
        str(session.binary),
        "claude",
        "--provider",
        claude_provider,
        "--enable-model-discovery",
    ]
    with AgentTerminal(session, "claude", provider_command, "configured-provider-models") as tui:
        tui.boot()
        tui.open_model_picker()
        provider_models = session.claude_gateway_model_ids("provider-models")
        tui.exit_normally()
    assert provider_models == [claude_provider_model]

    parent_command = [str(session.binary), "claude", "--parent", parent_schema]
    with AgentTerminal(session, "claude", parent_command, "configured-parent-models") as tui:
        tui.boot()
        tui.open_model_picker()
        parent_models = session.claude_gateway_model_ids("parent-models")
        tui.exit_normally()
    assert parent_models == [claude_parent_model]
    session.assert_not_routed()


@pytest.mark.tui
def test_ug_claude_bedrock_provider_discovers_only_claude_targets(
    live_session, workspace, bedrock_provider, bedrock_claude_model
):
    """Scenario: launch Claude with a mixed-model Bedrock MPS from a fresh home.

    Expected: Claude's real gateway catalog contains only its compatible
    Bedrock Claude target.
    """
    session = live_session

    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "--provider",
        bedrock_provider,
        "--enable-model-discovery",
    ]
    with AgentTerminal(session, "claude", command, "bedrock-provider-models") as tui:
        tui.boot()
        tui.open_model_picker()
        models = session.claude_gateway_model_ids()
        tui.exit_normally()

    assert models == [bedrock_claude_model]
    session.assert_not_routed()


def test_ug_claude_rejects_missing_provider(live_session, workspace):
    """Scenario: launch Claude with a provider name that does not exist.

    Expected: ug returns a clear not-found error before starting Claude.
    """
    missing = "does.not.exist"
    result = live_session.run(
        "claude",
        "--workspace",
        workspace,
        "--provider",
        missing,
        "--enable-model-discovery",
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    assert result.returncode == 1
    assert f"Model provider service '{missing}' was not found" in result.stdout + result.stderr


def test_ug_claude_rejects_wrong_provider_type(live_session, workspace, codex_provider):
    """Scenario: launch Claude with an OpenAI Model Provider Service.

    Expected: ug rejects the incompatible provider before starting Claude.
    """
    result = live_session.run(
        "claude",
        "--workspace",
        workspace,
        "--provider",
        codex_provider,
        "--enable-model-discovery",
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    assert result.returncode == 1
    assert "which claude can't route to" in result.stdout + result.stderr


def test_ug_claude_rejects_malformed_parent(live_session):
    """Scenario: launch Claude with a one-part parent value.

    Expected: ug rejects it as malformed before starting Claude.
    """
    result = live_session.run("claude", "--parent", "main", ok=False)

    assert result.returncode == 1
    assert "--parent must be `<catalog>.<schema>`" in result.stdout + result.stderr


def test_ug_claude_rejects_provider_and_parent_together(
    live_session, claude_provider, parent_schema
):
    """Scenario: launch Claude with both mutually exclusive discovery scopes.

    Expected: ug rejects the conflicting options before starting Claude.
    """
    result = live_session.run(
        "claude",
        "--provider",
        claude_provider,
        "--parent",
        parent_schema,
        ok=False,
    )

    assert result.returncode == 1
    assert "--provider and --parent cannot be used together" in result.stdout + result.stderr
