"""Claude managed-config CUJs for Tests-table cases 1, 3, 5, 7, 9, and 11."""

import re

import pytest
from utils.constants import MANAGED_CLAUDE_MODELS
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed, pytest.mark.claude]


def _claude_state_and_agent_files(session):
    paths = [session.home / ".claude.json"]
    for directory in (session.home / ".ucode", session.home / ".claude"):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return {
        str(path.relative_to(session.home)): path.read_bytes() for path in paths if path.is_file()
    }


def _configure_managed(session, workspace):
    result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in result.stdout, result.stdout


def _assert_rejected_before_claude_started(session, result, requested_source, before):
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert requested_source.lower() in output
    assert "admin has specified managed" in output
    assert _claude_state_and_agent_files(session) == before


def _assert_managed_models_in_picker(screen):
    expected = [model_id.removeprefix("system.ai.") for model_id in MANAGED_CLAUDE_MODELS]
    rendered = re.findall(r"(?m)^\s*(?:[❯›>]\s*)?\d+\.\s+(\S+)", screen)
    assert rendered == expected, screen


def _assert_no_claude_owned_gateway_cache_after_launch(session):
    """Check Claude Code's own cache only after its picker process has exited."""
    assert not (session.home / ".claude/cache/gateway-models.json").exists()


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
@pytest.mark.tui
def test_case_01_managed_claude_uses_admin_discovery_after_configure(
    live_session, workspace, configured
):
    """Scenario: launch managed Claude after configure and from fresh state.

    Expected: the managed model catalog wins in both command variants.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)

    args = [] if configured else ["--workspace", workspace]
    command = [str(session.binary), "claude", *args]
    with AgentTerminal(session, "claude", command, "case-01-managed") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
@pytest.mark.tui
def test_case_03_managed_claude_ignores_discovery_disable(live_session, workspace, configured):
    """Scenario: launch configured and fresh managed Claude with discovery disabled.

    Expected: workspace-managed discovery supplies the admin's catalog in both variants.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    args = [] if configured else ["--workspace", workspace]
    command = [str(session.binary), "claude", *args]
    with AgentTerminal(session, "claude", command, "case-03-managed-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_05_managed_claude_rejects_provider_override(
    live_session, workspace, claude_provider, configured
):
    """Scenario: pass --provider after managed configure and from fresh state.

    Expected: ug rejects both variants without changing state or Claude-owned cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    before = _claude_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "claude",
        *args,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_07_managed_claude_rejects_model_location_override(
    live_session, workspace, parent_schema, configured
):
    """Scenario: pass --model-location after managed configure and from fresh state.

    Expected: ug rejects both variants without changing state or Claude-owned cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    before = _claude_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "claude",
        *args,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_09_managed_claude_rejects_provider_when_discovery_disabled(
    live_session, workspace, claude_provider, configured
):
    """Scenario: disable discovery and pass --provider in both managed command variants.

    Expected: ug rejects both variants without changing state or Claude-owned cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "claude",
        *args,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_11_managed_claude_rejects_model_location_when_discovery_disabled(
    live_session, workspace, parent_schema, configured
):
    """Scenario: disable discovery and pass --model-location in both managed variants.

    Expected: ug rejects both variants without changing state or Claude-owned cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "claude",
        *args,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)
