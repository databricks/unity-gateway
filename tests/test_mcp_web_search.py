"""Tests for the web_search stdio MCP server."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from ucode import mcp_web_search

WS = "https://example.databricks.com"


def _drive(requests: list[dict]) -> list[dict]:
    """Run `serve()` over a synthetic stdin and return parsed responses."""
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    mcp_web_search.serve(stdin=stdin, stdout=stdout)
    out = stdout.getvalue().strip().splitlines()
    return [json.loads(line) for line in out]


class TestInitialize:
    def test_returns_protocol_and_server_info(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        assert len(responses) == 1
        result = responses[0]["result"]
        assert result["protocolVersion"] == mcp_web_search.PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == mcp_web_search.SERVER_NAME
        assert "tools" in result["capabilities"]


class TestNotifications:
    def test_initialized_notification_produces_no_response(self):
        responses = _drive([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        assert responses == []


class TestToolsList:
    def test_lists_web_search_tool(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        tools = responses[0]["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "web_search"
        assert "web" in tools[0]["description"].lower()
        assert tools[0]["inputSchema"]["required"] == ["query"]


class TestToolsCallSuccess:
    def test_unwraps_responses_api_text(self, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_call(query: str) -> dict:
            captured["query"] = query
            return {
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "output_text", "text": "ignored"}],
                    },
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Hello,"},
                            {"type": "output_text", "text": " world."},
                        ],
                    },
                ]
            }

        monkeypatch.setattr(mcp_web_search, "_call_responses_api", fake_call)
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "web_search", "arguments": {"query": "anthropic news"}},
                }
            ]
        )
        result = responses[0]["result"]
        assert "isError" not in result
        assert result["content"] == [{"type": "text", "text": "Hello,\n world."}]
        assert captured["query"] == "anthropic news"


class TestToolsCallErrors:
    def test_missing_query_returns_tool_error(self, monkeypatch):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "web_search", "arguments": {}},
                }
            ]
        )
        result = responses[0]["result"]
        assert result["isError"] is True
        assert "query" in result["content"][0]["text"].lower()

    def test_http_failure_surfaces_as_tool_error(self, monkeypatch):
        def boom(query: str) -> dict:
            raise RuntimeError("Responses API returned HTTP 500: oops")

        monkeypatch.setattr(mcp_web_search, "_call_responses_api", boom)
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "web_search", "arguments": {"query": "anything"}},
                }
            ]
        )
        result = responses[0]["result"]
        assert result["isError"] is True
        assert "HTTP 500" in result["content"][0]["text"]

    def test_empty_response_text_returns_tool_error(self, monkeypatch):
        monkeypatch.setattr(mcp_web_search, "_call_responses_api", lambda q: {"output": []})
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "web_search", "arguments": {"query": "x"}},
                }
            ]
        )
        assert responses[0]["result"]["isError"] is True

    def test_unknown_tool_name_returns_protocol_error(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "not_a_real_tool", "arguments": {}},
                }
            ]
        )
        assert "error" in responses[0]
        assert responses[0]["error"]["code"] == -32602


class TestProtocolErrors:
    def test_unknown_method(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 8, "method": "frobnicate"}])
        assert responses[0]["error"]["code"] == -32601

    def test_invalid_json_emits_parse_error(self):
        stdin = io.StringIO("not json\n")
        stdout = io.StringIO()
        mcp_web_search.serve(stdin=stdin, stdout=stdout)
        line = stdout.getvalue().strip()
        payload = json.loads(line)
        assert payload["error"]["code"] == -32700


class TestExtractText:
    def test_concatenates_all_message_text_chunks(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "a"},
                        {"type": "other", "text": "skip"},
                        {"type": "output_text", "text": "b"},
                    ],
                }
            ]
        }
        assert mcp_web_search._extract_response_text(payload) == "a\nb"

    def test_skips_non_message_items(self):
        payload = {
            "output": [
                {"type": "reasoning", "content": [{"type": "output_text", "text": "hidden"}]},
            ]
        }
        assert mcp_web_search._extract_response_text(payload) == ""


class TestCallResponsesApi:
    def test_missing_workspace_env(self, monkeypatch):
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "x")
        with pytest.raises(RuntimeError, match="DATABRICKS_HOST"):
            mcp_web_search._call_responses_api("query")

    def test_missing_model_env(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", WS)
        monkeypatch.delenv("UCODE_WEB_SEARCH_MODEL", raising=False)
        with pytest.raises(RuntimeError, match="UCODE_WEB_SEARCH_MODEL"):
            mcp_web_search._call_responses_api("query")

    def test_posts_to_responses_endpoint(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", WS)
        monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "databricks-gpt-5")
        monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda ws, profile=None: "tok")

        captured: dict[str, Any] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"output": []}).encode("utf-8")

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", fake_urlopen)
        mcp_web_search._call_responses_api("hello")

        assert captured["url"] == f"{WS}/ai-gateway/codex/v1/responses"
        assert captured["method"] == "POST"
        assert captured["body"]["model"] == "databricks-gpt-5"
        assert captured["body"]["tools"] == [{"type": "web_search"}]
        assert captured["body"]["input"] == [{"role": "user", "content": "hello"}]
        # urllib lowercases header names in header_items
        auth_header = next(
            v for k, v in captured["headers"].items() if k.lower() == "authorization"
        )
        assert auth_header == "Bearer tok"


class TestCacheOnlyRetry:
    """HIPAA/BAA workspaces reject live web search with a 400 that asks for
    `external_web_access: false`; the server retries once in cache-only mode
    and keeps using it (#805)."""

    HIPAA_DETAIL = (
        '{"error_code":"INVALID_PARAMETER_VALUE","message":"Web search with live internet '
        "access is not available for workspaces with HIPAA/BAA compliance enabled. Set "
        'external_web_access to false on the web_search tool for cache-only web search."}'
    )

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", WS)
        monkeypatch.setenv("UCODE_WEB_SEARCH_MODEL", "system.ai.gpt-5")
        monkeypatch.setattr(mcp_web_search, "get_databricks_token", lambda ws, profile=None: "tok")
        monkeypatch.setattr(mcp_web_search, "_cache_only_web_search", False)

    @staticmethod
    def _install(monkeypatch, responder):
        from urllib import error as urllib_error

        bodies: list[dict[str, Any]] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"output": []}).encode("utf-8")

        def fake_urlopen(req, timeout):
            body = json.loads(req.data.decode("utf-8"))
            bodies.append(body)
            status, detail = responder(body)
            if status == 200:
                return FakeResponse()
            raise urllib_error.HTTPError(
                req.full_url, status, "error", {}, io.BytesIO(detail.encode("utf-8"))
            )

        monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", fake_urlopen)
        return bodies

    def _hipaa_gateway(self, body):
        if body["tools"][0].get("external_web_access") is False:
            return 200, ""
        return 400, self.HIPAA_DETAIL

    def test_retries_with_external_web_access_false(self, monkeypatch):
        bodies = self._install(monkeypatch, self._hipaa_gateway)

        assert mcp_web_search._call_responses_api("hello") == {"output": []}

        assert [b["tools"] for b in bodies] == [
            [{"type": "web_search"}],
            [{"type": "web_search", "external_web_access": False}],
        ]

    def test_later_calls_go_straight_to_cache_only(self, monkeypatch):
        bodies = self._install(monkeypatch, self._hipaa_gateway)

        mcp_web_search._call_responses_api("first")
        mcp_web_search._call_responses_api("second")

        assert len(bodies) == 3
        assert bodies[2]["tools"] == [{"type": "web_search", "external_web_access": False}]

    def test_other_400s_are_not_retried(self, monkeypatch):
        bodies = self._install(monkeypatch, lambda body: (400, '{"message":"bad input"}'))

        with pytest.raises(RuntimeError, match="HTTP 400"):
            mcp_web_search._call_responses_api("hello")

        assert len(bodies) == 1
        assert mcp_web_search._cache_only_web_search is False

    def test_cache_only_failure_surfaces_error(self, monkeypatch):
        bodies = self._install(monkeypatch, lambda body: (400, self.HIPAA_DETAIL))

        with pytest.raises(RuntimeError, match="external_web_access"):
            mcp_web_search._call_responses_api("hello")

        assert len(bodies) == 2
