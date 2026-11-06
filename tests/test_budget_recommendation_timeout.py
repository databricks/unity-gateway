"""Component coverage for the advisory launch request, including a stalled local HTTP peer."""

from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from ucode import cli, databricks, managed_config, managed_resolve


def test_dry_run_does_not_start_advisory_request(monkeypatch):
    monkeypatch.setattr(cli, "is_dry_run", lambda: True)
    monkeypatch.setattr(
        cli, "get_databricks_token", lambda *_args: pytest.fail("dry-run must not acquire a token")
    )
    monkeypatch.setattr(
        cli,
        "get_model_recommendation",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not request a recommendation"),
    )

    assert (
        cli._fetch_budget_recommendation(
            {"workspace": "https://example.databricks.com"},
            {"smart_defaults": {"tiers": [{"spending_percentage": 0.8}]}},
        )
        is None
    )


def test_recommendation_client_preserves_default_timeout(monkeypatch):
    requests = []

    def post(url, token, payload, *, timeout):
        requests.append((url, token, payload, timeout))
        return {"recommended_model": "system.ai.example"}, None

    monkeypatch.setattr(databricks, "_http_post_json", post)
    result, reason = managed_config.get_model_recommendation(
        "https://example.databricks.com", "test-token"
    )

    assert reason is None
    assert result is not None
    assert requests == [
        (
            "https://example.databricks.com/api/ai-gateway/v2/coding-agent-configs:recommendModel",
            "test-token",
            {},
            30,
        )
    ]


def test_stalled_launch_request_warns_and_retains_configured_agent_and_model(monkeypatch):
    received = Event()
    release = Event()
    finished = Event()
    requests = []

    class StalledRecommendation(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, body))
            received.set()
            try:
                # The client must time out before the test lets the peer finish. This exercises
                # real socket behavior without asserting an elapsed-time performance threshold.
                release.wait(15)
            finally:
                finished.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StalledRecommendation)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    state = {"workspace": f"http://127.0.0.1:{server.server_port}"}
    managed = {
        "default_agent": "claude",
        "enabled_agents": {
            "claude": {"model_config": {"default_model": "system.ai.claude-sonnet-4-6"}}
        },
        "smart_defaults": {"tiers": [{"spending_percentage": 0.8}]},
    }
    warnings = []
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.delenv("UG_HEADERS", raising=False)
    monkeypatch.setattr(cli, "is_dry_run", lambda: False)
    monkeypatch.setattr(cli, "get_databricks_token", lambda *_args: "local-component-token")
    monkeypatch.setattr(cli, "spinner", lambda *_args: nullcontext())
    monkeypatch.setattr(cli, "print_warning", warnings.append)
    try:
        recommendation = cli._fetch_budget_recommendation(state, managed)

        assert received.is_set()
        assert not finished.is_set(), "the peer finished instead of the client timing out"
        assert recommendation is None
        assert len(warnings) == 1
        assert "Could not check your budget" in warnings[0]
        assert "timed out" in warnings[0]
        assert requests == [("/api/ai-gateway/v2/coding-agent-configs:recommendModel", b"{}")]
        assert managed_resolve.recommended_agent(recommendation, managed) == "claude"
        assert (
            managed_resolve.managed_launch_model(managed, recommendation, "claude")
            == "system.ai.claude-sonnet-4-6"
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()
        if received.is_set():
            assert finished.wait(5), "local request handler did not stop"
