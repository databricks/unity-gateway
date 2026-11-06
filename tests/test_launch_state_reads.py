"""Shared setup reuses reads within one invocation and sees edits on the next one."""

from __future__ import annotations

import copy
import json
from unittest.mock import Mock

import pytest

from ucode import cli, state, workspace
from ucode.agents import copilot
from ucode.databricks import GatewayProbe

SHARED = "https://shared.cloud.databricks.com"
SOURCE_KEY = workspace.workspace_key(SHARED, "111")
DESTINATION_KEY = workspace.workspace_key(SHARED, "222")
CUSTOM_OAUTH = {
    "client_id": "component-client",
    "redirect_url": "http://localhost:8020",
    "scopes": ["offline_access", "model-serving"],
}


@pytest.fixture
def setup_boundaries(monkeypatch):
    saved = []
    monkeypatch.setattr(cli, "resolve_workspace", lambda value: value)
    monkeypatch.setattr(cli, "save_state", lambda value: saved.append(copy.deepcopy(value)))
    monkeypatch.setattr(cli, "_purge_after_workspace_switch", Mock())
    monkeypatch.setattr(cli, "ensure_databricks_auth", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "get_databricks_token", lambda *args: "component-token")
    monkeypatch.setattr(cli, "ensure_custom_oauth_cli_token", lambda *args: "component-token")
    monkeypatch.setattr(
        cli,
        "probe_unity_gateway_capabilities",
        lambda *args: GatewayProbe(True, "component gateway", resource_available=True),
    )
    return saved


@pytest.mark.parametrize("skip_preflight", [False, True])
@pytest.mark.parametrize("oauth_action", ["keep", "clear", "replace"])
def test_current_selection_is_read_once_and_not_mutated(
    monkeypatch, setup_boundaries, skip_preflight, oauth_action
):
    previous = {
        "workspace": SHARED,
        "workspace_id": "111",
        "profile": "previous-profile",
        "gemini_models": ["saved-gemini"],
        "custom_oauth": CUSTOM_OAUTH,
    }
    original = copy.deepcopy(previous)
    load = Mock(return_value=previous)
    monkeypatch.setattr(cli, "load_state", load)
    kwargs = {}
    if oauth_action == "clear":
        kwargs["clear_custom_oauth"] = True
    elif oauth_action == "replace":
        kwargs["custom_oauth"] = {**CUSTOM_OAUTH, "client_id": "replacement-client"}

    result = cli.configure_shared_state(
        SOURCE_KEY,
        profile="selected-profile",
        tools=["gemini"],
        skip_model_discovery=True,
        skip_preflight=skip_preflight,
        **kwargs,
    )

    load.assert_called_once_with()
    assert previous == original
    assert result["profile"] == "selected-profile"
    assert result["gemini_models"] == ["saved-gemini"]
    assert setup_boundaries[-1]["workspace_id"] == "111"
    if oauth_action == "clear":
        assert "custom_oauth" not in result
    else:
        assert result["custom_oauth"] == kwargs.get("custom_oauth", CUSTOM_OAUTH)
    assert workspace.workspace_headers(SHARED) == workspace.selector_headers("111")


def test_switch_uses_one_destination_snapshot_for_mcp_catalog_and_self_management(
    monkeypatch, setup_boundaries
):
    source_managed = [{"name": "source-managed"}]
    previous = {
        "workspace": SHARED,
        "workspace_id": "111",
        "use_pat": True,
        "custom_oauth": CUSTOM_OAUTH,
        "managed_mcp_servers": source_managed,
        "mcp_servers": [{"name": "source-user"}],
        "mcp_clients": ["claude"],
        state.SELF_MANAGED_AGENTS_KEY: ["pi"],
    }
    original = copy.deepcopy(previous)
    destination = {
        "workspace_id": "222",
        "mcp_servers": [{"name": "destination-user"}],
        "mcp_clients": ["codex"],
        "claude_server_side_web_search": True,
        copilot.CATALOG_STATE_KEY: [{"id": "destination-model"}],
        state.SELF_MANAGED_AGENTS_KEY: ["codex"],
    }
    load = Mock(return_value=previous)
    full = Mock(return_value={"workspaces": {DESTINATION_KEY: destination}})
    monkeypatch.setattr(cli, "load_state", load)
    monkeypatch.setattr(cli, "load_full_state", full)
    monkeypatch.setattr(
        state, "load_full_state", lambda: pytest.fail("destination snapshot must be reused")
    )

    result = cli.configure_shared_state(
        DESTINATION_KEY, profile="destination-profile", skip_preflight=True
    )

    load.assert_called_once_with()
    full.assert_called_once_with()
    assert previous == original
    for field in destination:
        assert result[field] == destination[field]
    assert result["managed_mcp_servers"] == source_managed
    assert "use_pat" not in result
    assert "custom_oauth" not in result
    assert workspace.workspace_headers(SHARED) == workspace.selector_headers("222")
    cli._purge_after_workspace_switch.assert_called_once_with(
        result, SHARED, SOURCE_KEY, DESTINATION_KEY
    )


@pytest.mark.parametrize("alias", [False, True])
def test_destination_snapshot_keeps_bare_host_self_management_rules(monkeypatch, alias):
    block = {state.SELF_MANAGED_AGENTS_KEY: ["gemini"]}
    if alias:
        block[state.ALIAS_OF_KEY] = SOURCE_KEY
    snapshot = {"workspaces": {SHARED: block}}
    original = copy.deepcopy(snapshot)
    monkeypatch.setattr(state, "load_full_state", lambda: pytest.fail("must use supplied snapshot"))

    selected = state.workspace_self_managed_agents(DESTINATION_KEY, full_state=snapshot)

    assert selected == ([] if alias else ["gemini"])
    selected.append("pi")
    assert snapshot == original


def test_later_invocation_reads_external_state_changes(monkeypatch, setup_boundaries):
    def write_selection(model, client_id):
        state.STATE_PATH.write_text(
            json.dumps(
                {
                    "state_version": state.STATE_VERSION,
                    "current_workspace": SHARED,
                    state.CURRENT_WORKSPACE_KEY: SOURCE_KEY,
                    "workspaces": {
                        SOURCE_KEY: {
                            "workspace_id": "111",
                            "gemini_models": [model],
                            "custom_oauth": {**CUSTOM_OAUTH, "client_id": client_id},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    load = Mock(wraps=state.load_state)
    monkeypatch.setattr(cli, "load_state", load)
    write_selection("first-model", "first-client")
    first = cli.configure_shared_state(SOURCE_KEY, profile="p", skip_preflight=True)
    write_selection("edited-model", "edited-client")
    second = cli.configure_shared_state(SOURCE_KEY, profile="p", skip_preflight=True)

    assert load.call_count == 2
    assert first["gemini_models"] == ["first-model"]
    assert first["custom_oauth"]["client_id"] == "first-client"
    assert second["gemini_models"] == ["edited-model"]
    assert second["custom_oauth"]["client_id"] == "edited-client"
