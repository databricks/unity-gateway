"""Launch-scoped discovery with a real local HTTP server as the external gateway."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ucode import databricks


@pytest.fixture
def catalog_endpoint():
    requests = []
    outcomes = []
    payload = {
        "data": [
            {
                "id": "databricks-claude-sonnet-4",
                "display_name": "Sonnet",
                "description": "Test model",
            }
        ]
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(dict(self.headers))
            status, data = outcomes.pop(0) if outcomes else (200, payload)
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, outcomes, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_launch_family_discovery_and_picker_share_one_response(catalog_endpoint):
    workspace, requests, _, _ = catalog_endpoint

    with databricks.model_discovery_session():
        families, reason = databricks.discover_claude_models(workspace, "test-token")
        catalog = databricks.list_anthropic_model_catalog(workspace, "test-token")

    assert reason is None
    assert families == {"sonnet": "databricks-claude-sonnet-4"}
    assert catalog.model_ids == list(families.values())
    assert catalog.model_id_to_display_name == {"databricks-claude-sonnet-4": "Sonnet"}
    assert catalog.model_id_to_description == {"databricks-claude-sonnet-4": "Test model"}
    assert len(requests) == 1


def test_next_launch_fetches_changed_catalog(catalog_endpoint):
    workspace, requests, _, payload = catalog_endpoint
    with databricks.model_discovery_session():
        first = databricks.list_anthropic_model_catalog(workspace, "test-token")
    payload["data"] = [{"id": "new-model", "display_name": "New model"}]
    with databricks.model_discovery_session():
        second = databricks.list_anthropic_model_catalog(workspace, "test-token")

    assert first.model_ids == ["databricks-claude-sonnet-4"]
    assert second.model_ids == ["new-model"]
    assert len(requests) == 2


@pytest.mark.parametrize("failure", [(403, {}), (503, {}), (200, {"unexpected": []})])
def test_failure_is_not_remembered(catalog_endpoint, failure):
    workspace, requests, outcomes, _ = catalog_endpoint
    outcomes.append(failure)
    with databricks.model_discovery_session():
        first = databricks.list_anthropic_model_catalog(workspace, "test-token")
        second = databricks.list_anthropic_model_catalog(workspace, "test-token")

    assert first.error_msg
    assert second.model_ids == ["databricks-claude-sonnet-4"]
    assert second.error_msg is None
    assert len(requests) == 2


def test_different_authentication_and_catalog_sources_do_not_share(catalog_endpoint, monkeypatch):
    workspace, requests, _, _ = catalog_endpoint
    with databricks.model_discovery_session():
        for token, scope in [
            ("token-a", {}),
            ("token-b", {}),
            ("token-a", {"provider": "main.team.provider"}),
            ("token-a", {"parent_schema": "main.team"}),
        ]:
            for _ in range(2):
                catalog = databricks.list_anthropic_model_catalog(workspace, token, **scope)
                assert catalog.error_msg is None
        monkeypatch.setenv("UG_HEADERS", "X-Databricks-Org-Id: 123")
        databricks.list_anthropic_model_catalog(workspace, "token-a")

    assert len(requests) == 5
    assert {r["Authorization"] for r in requests} == {"Bearer token-a", "Bearer token-b"}


def test_workspace_hosts_do_not_share(catalog_endpoint):
    workspace, requests, _, _ = catalog_endpoint
    with databricks.model_discovery_session():
        for host in [workspace, workspace.replace("127.0.0.1", "localhost")]:
            for _ in range(2):
                assert databricks.list_anthropic_model_catalog(host, "test-token").model_ids
    assert len(requests) == 2


def test_scope_resets_after_failure_and_outside_launches(catalog_endpoint):
    workspace, requests, _, _ = catalog_endpoint
    with pytest.raises(RuntimeError, match="launch failed"):
        with databricks.model_discovery_session():
            databricks.list_anthropic_model_catalog(workspace, "test-token")
            raise RuntimeError("launch failed")
    for _ in range(2):
        databricks.list_anthropic_model_catalog(workspace, "test-token")
    assert len(requests) == 3


def test_successful_empty_catalog_is_reused_without_changing_error(catalog_endpoint):
    workspace, requests, _, payload = catalog_endpoint
    payload["data"] = []
    with databricks.model_discovery_session():
        first = databricks.list_anthropic_model_catalog(workspace, "test-token")
        second = databricks.list_anthropic_model_catalog(workspace, "test-token")
    assert first == second
    assert first.error_msg == "AI Gateway returned no Anthropic model ids"
    assert len(requests) == 1
