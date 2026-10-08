"""Offline checks for read-only CUJ workspace configuration and catalog access."""

import copy
from types import SimpleNamespace

import pytest
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from tests.e2e_cuj import base
from tests.e2e_cuj.base import BaseCujTest
from tests.e2e_cuj.helpers.constants import CLAUDE, CODEX
from tests.e2e_cuj.helpers.workspace import Workspace


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Config, "_resolve_host_metadata", lambda self: None, raising=False)
    return WorkspaceClient(host="https://example.test", token="test-token", auth_type="pat")


def test_cuj_workspace_auth_uses_each_class_selected_credentials(monkeypatch):
    calls = []

    class CapturingClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    class Shared(BaseCujTest):
        WORKSPACE_URL = "https://shared.example"

    class Budget(BaseCujTest):
        WORKSPACE_URL = "https://budget.example"
        CLIENT_ID_ENV = "UG_BUDGET_CUJ_SP_CLIENT_ID"
        CLIENT_SECRET_ENV = "UG_BUDGET_CUJ_SP_CLIENT_SECRET"

    monkeypatch.setattr(base, "WorkspaceClient", CapturingClient)
    monkeypatch.setenv("UG_CUJ_SP_CLIENT_ID", "shared-id")
    monkeypatch.setenv("UG_CUJ_SP_CLIENT_SECRET", "shared-secret")
    monkeypatch.setenv("UG_BUDGET_CUJ_SP_CLIENT_ID", "budget-id")
    monkeypatch.setenv("UG_BUDGET_CUJ_SP_CLIENT_SECRET", "budget-secret")

    setup_workspace = BaseCujTest.setup_workspace.__wrapped__
    setup_workspace(None, SimpleNamespace(cls=Shared))
    setup_workspace(None, SimpleNamespace(cls=Budget))

    assert calls == [
        {
            "host": "https://shared.example",
            "client_id": "shared-id",
            "client_secret": "shared-secret",
            "auth_type": "oauth-m2m",
        },
        {
            "host": "https://budget.example",
            "client_id": "budget-id",
            "client_secret": "budget-secret",
            "auth_type": "oauth-m2m",
        },
    ]


def test_cuj_catalog_rejects_unknown_agent_before_network_access(client, monkeypatch):
    workspace = Workspace(client)

    def unexpected_request(*args, **kwargs):
        pytest.fail("Unsupported agents must be rejected before network access")

    monkeypatch.setattr(client.api_client, "do", unexpected_request)
    with pytest.raises(ValueError, match="Unsupported agent"):
        workspace.model_ids("unsupported")


@pytest.mark.parametrize("changed", [False, True])
def test_cuj_workspace_reads_shared_config_and_never_repairs_it(client, monkeypatch, changed):
    published = {"name": "coding-agent-configs/test", "spec_version": 1, "enabled_agents": []}
    calls = []

    def request(method, path):
        assert method == "GET"
        assert path == "/api/ai-gateway/v2/coding-agent-configs"
        calls.append(path)
        return {"coding_agent_configs": [copy.deepcopy(published)]}

    monkeypatch.setattr(client.api_client, "do", request)
    first, second = Workspace(client), Workspace(client)
    baseline = first.config()
    assert second.config() == baseline
    published["update_time"] = "new server timestamp"
    if changed:
        published["spec_version"] = 2
        with pytest.raises(AssertionError, match="configuration changed"):
            first.assert_unchanged(baseline)
        assert published["spec_version"] == 2, "Never restore or repair workspace configuration"
    else:
        first.assert_unchanged(baseline)
    assert len(calls) == 3


@pytest.mark.parametrize("agent", [CLAUDE, CODEX])
def test_cuj_catalog_uses_sdk_queries_and_pagination(client, monkeypatch, agent):
    workspace = Workspace(client)
    calls = []

    def request(method, path, *, query):
        calls.append((path, query))
        assert method == "GET"
        if path == "/ai-gateway/anthropic/v1/models":
            assert query == {"limit": 1000}
            return {"data": [{"id": "system.ai.claude-sonnet-4-6"}], "has_more": False}
        assert path == "/api/2.1/unity-catalog/model-services"
        assert query["parent"] == "schemas/system.ai" and query["page_size"] == "100"
        if "page_token" not in query:
            return {
                "model_services": [{"name": "model-services/system.ai.claude-sonnet-4-6"}],
                "next_page_token": "second-page",
            }
        assert query["page_token"] == "second-page"
        return {"model_services": [{"name": "model-services/system.ai.gpt-5-6-sol"}]}

    monkeypatch.setattr(client.api_client, "do", request)
    expected = {CLAUDE: "system.ai.claude-sonnet-4-6", CODEX: "system.ai.gpt-5-6-sol"}
    assert workspace.model_ids(agent) == {expected[agent]}
    assert len(calls) == (3 if agent == CLAUDE else 2)
