"""Claude managed-config CUJs for repository scenarios 1, 3, and 5.

The admin CodingAgentConfig is fetched once from the managed workspace, its Claude model source is
set to the dedicated test MPS, and the result is reused through ``UCODE_MANAGED_CONFIG_STUB`` in
each isolated session. Normalization, config writers, the gateway, and Claude Code remain real.
"""

import json
import os

import pytest
from utils.constants import MANAGED_CLAUDE_PROVIDER_SERVICE
from utils.managed import (
    fetch_managed_config_stub,
    is_managed_config_control_plane_cache,
    use_managed_config_stub,
)
from utils.model_discovery import claude_model_in_picker
from utils.provider_catalog import (
    AnthropicProviderCatalog,
    fetch_anthropic_provider_catalog,
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


@pytest.fixture(scope="module")
def _managed_claude_provider_catalog(workspace):
    return fetch_anthropic_provider_catalog(
        workspace,
        os.environ["DATABRICKS_BEARER"],
        MANAGED_CLAUDE_PROVIDER_SERVICE,
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


def _assert_managed_provider_in_picker(
    session, workspace, screen, expected: AnthropicProviderCatalog
):
    settings = json.loads((session.home / ".claude" / "ucode-settings.json").read_text())
    headers = (settings.get("env") or {}).get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
    expected_header = f"Databricks-Model-Provider-Service: {MANAGED_CLAUDE_PROVIDER_SERVICE}"
    assert headers.count(expected_header) == 1, settings
    # Without authored defaults, replace built-in rows with the discovered MPS catalog.
    assert not {"availableModels", "enforceAvailableModels"} & settings.keys(), settings
    picker = settings["modelPicker"]
    assert picker["replaceBuiltInOptions"] is True, picker
    assert sorted(option["model"] for option in picker["options"]) == sorted(expected.model_ids)
    for option in picker["options"]:
        if display_name := expected.display_names.get(option["model"]):
            assert option["label"] == display_name, option
    cache = json.loads((session.home / ".claude/cache/gateway-models.json").read_text())
    assert cache.get("baseUrl") == workspace.rstrip("/") + "/ai-gateway/anthropic", cache
    assert isinstance(cache.get("fetchedAt"), int) and cache["fetchedAt"] > 0, cache
    cached_models = cache.get("models")
    assert isinstance(cached_models, list) and cached_models, cache
    cached_by_id = {}
    for model in cached_models:
        assert isinstance(model, dict), cache
        model_id = model.get("id")
        assert isinstance(model_id, str) and model_id, cache
        assert model_id not in cached_by_id, cache
        cached_by_id[model_id] = model.get("display_name")
    cached_ids = list(cached_by_id)
    session.record(
        "managed-provider-catalog.json",
        {
            "provider": MANAGED_CLAUDE_PROVIDER_SERVICE,
            "expected_model_ids": list(expected.model_ids),
            "expected_display_names": expected.display_names,
            "cached_model_ids": cached_ids,
        },
    )
    session.record("managed-gateway-cache.json", cache)
    assert sorted(cached_ids) == sorted(expected.model_ids), (cached_ids, expected.model_ids)
    assert any(
        claude_model_in_picker(
            screen,
            model_id,
            expected.display_names.get(model_id) or cached_by_id.get(model_id),
        )
        for model_id in expected.model_ids
    ), screen


@pytest.mark.tui
def test_case_01_managed_claude_uses_admin_discovery_after_configure(
    live_session, workspace, _managed_claude_provider_catalog
):
    """Scenario: configure managed Claude, then launch its model picker.

    Expected: the independently fetched provider catalog matches Claude's gateway cache and
    replacement picker, and at least one expected model is visible in a numbered picker row.
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
        screen = tui.open_model_picker(
            model_visible=lambda text: any(
                claude_model_in_picker(
                    text, model_id, _managed_claude_provider_catalog.display_names[model_id]
                )
                for model_id in _managed_claude_provider_catalog.model_ids
            )
        )
        tui.exit_normally()

    _assert_managed_provider_in_picker(session, workspace, screen, _managed_claude_provider_catalog)


@pytest.mark.tui
def test_case_01_fresh_managed_claude_uses_admin_discovery(
    live_session, workspace, _managed_claude_provider_catalog
):
    """Scenario: launch managed Claude's model picker from fresh state.

    Expected: the independently fetched provider catalog matches Claude's gateway cache and
    replacement picker, and at least one expected model is visible in a numbered picker row.
    """
    session = live_session
    command = [str(session.binary), "claude", "--workspace", workspace]
    with AgentTerminal(session, "claude", command, "case-01-fresh-managed") as tui:
        tui.boot()
        screen = tui.open_model_picker(
            model_visible=lambda text: any(
                claude_model_in_picker(
                    text, model_id, _managed_claude_provider_catalog.display_names[model_id]
                )
                for model_id in _managed_claude_provider_catalog.model_ids
            )
        )
        tui.exit_normally()

    _assert_managed_provider_in_picker(session, workspace, screen, _managed_claude_provider_catalog)


def test_case_03_managed_claude_rejects_provider_override(live_session, workspace, claude_provider):
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


def test_case_03_fresh_managed_claude_rejects_provider_override(
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


def test_case_05_managed_claude_rejects_model_location_override(
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


def test_case_05_fresh_managed_claude_rejects_model_location_override(
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
