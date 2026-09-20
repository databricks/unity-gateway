"""Claude managed-config CUJs for Tests-table cases 1, 3, 5, 7, 9, and 11.

The admin CodingAgentConfig is fetched once from the managed workspace, its Claude model source is
set to the dedicated test MPS, and the result is reused through ``UCODE_MANAGED_CONFIG_STUB`` in
each isolated session. Normalization, config writers, the gateway, and Claude Code remain real.
"""

import json
import os
import re
import time

import pytest
from utils.constants import MANAGED_CLAUDE_PROVIDER_SERVICE
from utils.managed import (
    fetch_managed_config_stub,
    is_managed_config_control_plane_cache,
    use_managed_config_stub,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.managed_fixture, pytest.mark.claude]


@pytest.fixture(scope="module")
def _managed_claude_config_stub(workspace, tmp_path_factory):
    return fetch_managed_config_stub(
        workspace,
        os.environ["DATABRICKS_BEARER"],
        tmp_path_factory.mktemp("managed-config-claude"),
        "managed-config-claude.json",
        agent="CODING_AGENT_CLAUDE_CODE",
        provider_service=MANAGED_CLAUDE_PROVIDER_SERVICE,
    )


@pytest.fixture(autouse=True)
def _managed_claude_config(live_session, _managed_claude_config_stub):
    use_managed_config_stub(live_session, _managed_claude_config_stub)


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


def _assert_rejected_before_claude_started(session, result, before=None):
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "`--provider` or `--model-location` is not allowed" in output
    assert "managed config exists for the workspace" in output
    if before is not None:
        assert _claude_state_and_agent_files(session) == before


def _assert_managed_provider_in_picker(session, workspace, screen):
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    headers = (settings.get("env") or {}).get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
    expected_header = f"Databricks-Model-Provider-Service: {MANAGED_CLAUDE_PROVIDER_SERVICE}"
    assert headers.count(expected_header) == 1, settings
    # MPS models come from Claude Code's native gateway discovery, not a static managed picker.
    assert not {"availableModels", "enforceAvailableModels", "modelPicker"} & settings.keys(), (
        settings
    )
    # Native gateway rows deduplicate against built-ins, so exact cached ids need not be rendered.
    assert re.search(r"(?m)^\s*(?:[❯›>]\s*)?\d+\.\s+\S", screen), screen

    cache = json.loads((session.home / ".claude/cache/gateway-models.json").read_text())
    assert cache.get("baseUrl") == workspace.rstrip("/") + "/ai-gateway/anthropic", cache
    assert isinstance(cache.get("fetchedAt"), int) and cache["fetchedAt"] > 0, cache
    cached_models = cache.get("models")
    assert isinstance(cached_models, list) and cached_models, cache
    cached_ids = [model.get("id") for model in cached_models if isinstance(model, dict)]
    assert len(cached_ids) == len(cached_models), cache
    assert cached_ids and all(isinstance(model_id, str) and model_id for model_id in cached_ids), (
        cache
    )


@pytest.mark.tui
def test_case_01_managed_claude_uses_admin_discovery_after_configure(live_session, workspace):
    """Scenario: configure managed Claude, open its model picker, then restart.

    Expected: the managed model catalog wins, a fresh cache replaces the prior
    session's cache on restart, and the model picker opens again. No inference is tested.
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

    _assert_managed_provider_in_picker(session, workspace, screen)

    cache_path = session.home / ".claude/cache/gateway-models.json"
    previous = json.loads(cache_path.read_text())
    restarted_at = time.time_ns() // 1_000_000
    with AgentTerminal(session, "claude", command, "case-01-restarted") as tui:
        tui.boot()
        refreshed = json.loads(cache_path.read_text())
        assert refreshed["fetchedAt"] >= restarted_at > previous["fetchedAt"]
        assert refreshed["baseUrl"] == previous["baseUrl"]
        assert refreshed["models"]
        screen = tui.open_model_picker()
        tui.exit_normally()
    _assert_managed_provider_in_picker(session, workspace, screen)


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

    _assert_managed_provider_in_picker(session, workspace, screen)


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

    _assert_managed_provider_in_picker(session, workspace, screen)


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

    _assert_managed_provider_in_picker(session, workspace, screen)


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

    _assert_rejected_before_claude_started(session, result, before)


def test_case_05_fresh_managed_claude_rejects_provider_override(
    live_session, workspace, claude_provider
):
    """Scenario: pass --provider while launching managed Claude from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Claude.
    """
    session = live_session
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

    _assert_rejected_before_claude_started(session, result)


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

    _assert_rejected_before_claude_started(session, result, before)


def test_case_07_fresh_managed_claude_rejects_model_location_override(
    live_session, workspace, parent_schema
):
    """Scenario: pass --model-location while launching managed Claude from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Claude.
    """
    session = live_session
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

    _assert_rejected_before_claude_started(session, result)


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

    _assert_rejected_before_claude_started(session, result, before)


def test_case_09_fresh_managed_claude_rejects_provider_when_discovery_disabled(
    live_session, workspace, claude_provider
):
    """Scenario: disable discovery and pass --provider to managed Claude from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Claude.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
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

    _assert_rejected_before_claude_started(session, result)


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

    _assert_rejected_before_claude_started(session, result, before)


def test_case_11_fresh_managed_claude_rejects_model_location_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: disable discovery and pass --model-location to managed Claude from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Claude.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
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

    _assert_rejected_before_claude_started(session, result)
