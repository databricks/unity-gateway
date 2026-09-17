"""Claude managed-config CUJs for Tests-table cases 1, 3, 5, 7, 9, and 11.

The admin CodingAgentConfig input is injected through ``UCODE_MANAGED_CONFIG_STUB``. Authentication,
normalization, config writers, the gateway, and Claude Code remain real. The un-stubbed managed
configure journey covers the fetch/wire contract.
"""

import re

import pytest
from utils.constants import MANAGED_FIXTURE_CLAUDE_MODELS
from utils.managed import (
    build_claude_agent_config,
    build_coding_agent_config,
    is_managed_config_control_plane_cache,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed_fixture, pytest.mark.claude]

CLAUDE_MANAGED_CONFIG = build_coding_agent_config(
    "CODING_AGENT_CLAUDE_CODE",
    build_claude_agent_config(MANAGED_FIXTURE_CLAUDE_MODELS),
)


@pytest.fixture(autouse=True)
def _managed_claude_config(live_session, tmp_path):
    set_managed_config_stub(live_session, tmp_path, CLAUDE_MANAGED_CONFIG)


def _claude_state_and_agent_files(session):
    paths = [session.home / ".claude.json"]
    for directory in (session.home / ".ucode", session.home / ".claude"):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return {
        str(path.relative_to(session.home)): path.read_bytes()
        for path in paths
        if path.is_file()
        # A fresh launch must retrieve and cache the control-plane input before it can reject an
        # override. Exclude only that expected cache; every agent-owned state/file stays compared.
        and not is_managed_config_control_plane_cache(session.home, path)
    }


def _assert_rejected_before_claude_started(session, result, requested_source, before):
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert requested_source.lower() in output
    assert "admin has specified managed" in output
    assert _claude_state_and_agent_files(session) == before


def _assert_managed_models_in_picker(screen):
    expected = [
        (model_id.removeprefix("system.ai."), model_id)
        for model_id in MANAGED_FIXTURE_CLAUDE_MODELS
    ]
    rendered = re.findall(
        r"(?m)^\s*(?:[❯›>]\s*)?\d+\.\s+(\S+)[^\n]*?"
        r"Managed by your organization\s+\(([^)\n]+)\)\s*$",
        screen,
    )
    assert rendered == expected, screen


def _assert_no_claude_owned_gateway_cache_after_launch(session):
    """Check Claude Code's own cache only after its picker process has exited."""
    assert not (session.home / ".claude/cache/gateway-models.json").exists()


@pytest.mark.tui
def test_case_01_managed_claude_uses_admin_discovery_after_configure(live_session, workspace):
    """Scenario: configure managed Claude, then launch its model picker.

    Expected: the managed model catalog wins after configuration.
    """
    session = live_session
    result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in result.stdout, result.stdout

    command = [str(session.binary), "claude"]
    with AgentTerminal(session, "claude", command, "case-01-managed") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


@pytest.mark.tui
def test_case_01_fresh_managed_claude_uses_admin_discovery(live_session, workspace):
    """Scenario: launch managed Claude's model picker from fresh state.

    Expected: the managed model catalog wins without prior configuration.
    """
    session = live_session
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-01-fresh-managed") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


@pytest.mark.tui
def test_case_03_managed_claude_ignores_discovery_disable_after_configure(live_session, workspace):
    """Scenario: configure managed Claude, disable discovery, then launch its model picker.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = live_session
    result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in result.stdout, result.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    command = [str(session.binary), "claude"]
    with AgentTerminal(session, "claude", command, "case-03-managed-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


@pytest.mark.tui
def test_case_03_fresh_managed_claude_ignores_discovery_disable(live_session, workspace):
    """Scenario: disable discovery and launch managed Claude's model picker from fresh state.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-03-fresh-managed-disabled") as tui:
        tui.boot()
        screen = tui.open_model_picker()
        tui.exit_normally()

    _assert_managed_models_in_picker(screen)
    _assert_no_claude_owned_gateway_cache_after_launch(session)


def test_case_05_managed_claude_rejects_provider_override(live_session, workspace, claude_provider):
    """Scenario: configure managed Claude, then pass --provider.

    Expected: ug rejects the override without changing agent-owned state/files.
    """
    session = live_session
    configured_result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured_result.stdout
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


def test_case_05_fresh_managed_claude_rejects_provider_override(
    live_session, workspace, claude_provider
):
    """Scenario: pass --provider while launching managed Claude from fresh state.

    Expected: ug rejects the override without changing agent-owned state/files; only the
    managed-config retrieval cache may be written.
    """
    session = live_session
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


def test_case_07_managed_claude_rejects_model_location_override(
    live_session, workspace, parent_schema
):
    """Scenario: configure managed Claude, then pass --model-location.

    Expected: ug rejects the override without changing agent-owned state/files.
    """
    session = live_session
    configured_result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured_result.stdout
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)


def test_case_07_fresh_managed_claude_rejects_model_location_override(
    live_session, workspace, parent_schema
):
    """Scenario: pass --model-location while launching managed Claude from fresh state.

    Expected: ug rejects the override without changing agent-owned state/files; only the
    managed-config retrieval cache may be written.
    """
    session = live_session
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)


def test_case_09_managed_claude_rejects_provider_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: configure managed Claude, disable discovery, then pass --provider.

    Expected: ug rejects the override without changing agent-owned state/files.
    """
    session = live_session
    configured_result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured_result.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


def test_case_09_fresh_managed_claude_rejects_provider_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: disable discovery and pass --provider to managed Claude from fresh state.

    Expected: ug rejects the override without changing agent-owned state/files; only the
    managed-config retrieval cache may be written.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, f"provider {claude_provider}", before)


def test_case_11_managed_claude_rejects_model_location_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: configure managed Claude, disable discovery, then pass --model-location.

    Expected: ug rejects the override without changing agent-owned state/files.
    """
    session = live_session
    configured_result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured_result.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)


def test_case_11_fresh_managed_claude_rejects_model_location_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: disable discovery and pass --model-location to managed Claude from fresh state.

    Expected: ug rejects the override without changing agent-owned state/files; only the
    managed-config retrieval cache may be written.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _claude_state_and_agent_files(session)
    result = session.run(
        "claude",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_claude_started(session, result, "--model-location", before)
