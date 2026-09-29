"""Tests for the web_search stdio MCP server."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from databricks.sdk import oauth

from ucode import databricks, mcp_web_search
from ucode.agents import claude

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


class TestConfiguredSearchAuthentication:
    @pytest.fixture(autouse=True)
    def isolated_auth(self, monkeypatch, tmp_path):
        # Only auth/network boundaries are replaced; registration, MCP dispatch, and token
        # selection execute normally against an SDK cache isolated from developer credentials.
        for name in (
            "DATABRICKS_BEARER",
            "DATABRICKS_BEARER_COMMAND",
            "DATABRICKS_CONFIG_PROFILE",
            "ENABLE_CUSTOM_OAUTH_FROM_CLI",
            "UCODE_WEB_SEARCH_CLIENT_ID",
            "UCODE_WEB_SEARCH_REDIRECT_URL",
            "UCODE_WEB_SEARCH_SCOPES",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "empty-databrickscfg"))
        monkeypatch.setattr(oauth.TokenCache, "BASE_PATH", str(tmp_path / "oauth"))
        self.endpoints = oauth.OidcEndpoints(
            authorization_endpoint=f"{WS}/oidc/v1/authorize",
            token_endpoint=f"{WS}/oidc/v1/token",
        )
        monkeypatch.setattr(oauth, "get_workspace_endpoints", lambda host: self.endpoints)
        self.browser = Mock(side_effect=AssertionError("Search cannot start browser consent"))
        monkeypatch.setattr(oauth.Consent, "launch_external_browser", self.browser)
        monkeypatch.setattr(
            databricks, "run", Mock(side_effect=AssertionError("Unexpected CLI auth"))
        )
        self.auth_headers = []

        def search_response(request, timeout):
            self.auth_headers.append(request.get_header("Authorization"))
            return io.BytesIO(
                json.dumps(
                    {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "Search result"}],
                            }
                        ]
                    }
                ).encode()
            )

        monkeypatch.setattr(mcp_web_search.urllib_request, "urlopen", search_response)

    def configure(self, monkeypatch, custom_oauth=None):
        entry = claude._web_search_mcp_entry(
            WS, "search-model", "workspace-profile", custom_oauth=custom_oauth
        )
        for name, value in entry["env"].items():
            monkeypatch.setenv(name, value)

    def search(self):
        return _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "web_search", "arguments": {"query": "query"}},
                }
            ]
        )[0]["result"]

    @pytest.mark.parametrize("expired", [False, True])
    def test_search_uses_custom_sdk_cache_and_refresh(self, monkeypatch, expired):
        config = {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "all-apis"],
        }
        cache = oauth.TokenCache(host=WS, oidc_endpoints=self.endpoints, **config)
        cache.save(
            oauth.SessionCredentials(
                token=oauth.Token(
                    access_token="cached-token",
                    token_type="Bearer",
                    refresh_token="refresh-token",
                    expiry=datetime.now(UTC) + timedelta(hours=1),
                ),
                token_endpoint=self.endpoints.token_endpoint,
                client_id=config["client_id"],
                redirect_url=config["redirect_url"],
            )
        )
        if expired:
            cache_path = Path(cache.filename)
            payload = json.loads(cache_path.read_text())
            payload["token"]["expiry"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
            cache_path.write_text(json.dumps(payload))
        refresh = Mock(
            return_value=oauth.Token(
                access_token="refreshed-token",
                token_type="Bearer",
                refresh_token="rotated-refresh",
                expiry=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        monkeypatch.setattr(oauth, "retrieve_token", refresh)
        # The harness custom SDK helper also prefers its own cache over an unrelated bearer.
        monkeypatch.setenv("DATABRICKS_BEARER", "unrelated-bearer")
        self.configure(monkeypatch, config)

        assert self.search() == {"content": [{"type": "text", "text": "Search result"}]}
        assert self.search() == {"content": [{"type": "text", "text": "Search result"}]}
        expected = "refreshed-token" if expired else "cached-token"
        assert self.auth_headers == [f"Bearer {expected}"] * 2
        assert refresh.call_count == int(expired)
        self.browser.assert_not_called()

    @pytest.mark.parametrize("custom_profile", [False, True])
    def test_search_refreshes_through_selected_cli_profile(self, monkeypatch, custom_profile):
        config = (
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
                "profile": "custom-profile",
            }
            if custom_profile
            else None
        )
        self.configure(monkeypatch, config)
        calls = []

        def cli_token(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"access_token": f"token-{len(calls)}"}), ""
            )

        monkeypatch.setattr(databricks, "run", cli_token)
        assert self.search() == {"content": [{"type": "text", "text": "Search result"}]}
        assert self.search() == {"content": [{"type": "text", "text": "Search result"}]}
        assert self.auth_headers == ["Bearer token-1", "Bearer token-2"]
        expected_profile = "custom-profile" if custom_profile else "workspace-profile"
        assert [command[command.index("--profile") + 1] for command in calls] == [
            expected_profile,
            expected_profile,
        ]

    def test_missing_custom_credentials_returns_error_without_default_auth(self, monkeypatch):
        self.configure(
            monkeypatch,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
            },
        )
        result = self.search()
        assert result["isError"] is True
        assert "run `ug claude`" in result["content"][0]["text"]
        assert self.auth_headers == []
        self.browser.assert_not_called()

    @pytest.mark.parametrize(
        "missing_name",
        ["UCODE_WEB_SEARCH_CLIENT_ID", "UCODE_WEB_SEARCH_REDIRECT_URL", "UCODE_WEB_SEARCH_SCOPES"],
    )
    def test_incomplete_custom_config_fails_closed(self, monkeypatch, missing_name):
        self.configure(
            monkeypatch,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
            },
        )
        monkeypatch.delenv(missing_name)
        result = self.search()
        assert result["isError"] is True
        assert "Failed to acquire Databricks token" in result["content"][0]["text"]
        assert self.auth_headers == []

    @pytest.mark.parametrize(
        "name,value",
        [
            ("UCODE_WEB_SEARCH_REDIRECT_URL", "https://example.invalid/callback"),
            ("UCODE_WEB_SEARCH_SCOPES", "offline_access"),
        ],
    )
    def test_invalid_custom_config_returns_tool_error(self, monkeypatch, name, value):
        self.configure(
            monkeypatch,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
            },
        )
        monkeypatch.setenv(name, value)
        result = self.search()
        assert result["isError"] is True
        assert "Failed to acquire Databricks token" in result["content"][0]["text"]
        assert self.auth_headers == []

    def test_cli_auth_timeout_returns_tool_error_without_browser(self, monkeypatch):
        self.configure(
            monkeypatch,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
                "profile": "custom-profile",
            },
        )
        calls = []

        def timed_out(command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        monkeypatch.setattr(databricks, "run", timed_out)
        result = self.search()
        assert result["isError"] is True
        assert "Failed to acquire Databricks token" in result["content"][0]["text"]
        assert self.auth_headers == []
        assert [timeout for _, timeout in calls] == [15, 30, 15]
        assert "--no-browser" in calls[1][0]
        assert all(
            command[command.index("--profile") + 1] == "custom-profile" for command, _ in calls
        )

    @pytest.mark.parametrize("custom_profile", [False, True])
    def test_profile_auth_preserves_explicit_bearer_override(self, monkeypatch, custom_profile):
        self.configure(
            monkeypatch,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "all-apis"],
                "profile": "custom-profile",
            }
            if custom_profile
            else None,
        )
        monkeypatch.setenv("DATABRICKS_BEARER", "explicit-bearer")
        assert self.search() == {"content": [{"type": "text", "text": "Search result"}]}
        assert self.auth_headers == ["Bearer explicit-bearer"]
