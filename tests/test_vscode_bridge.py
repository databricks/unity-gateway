"""Tests for `ug vscode-bridge`: its secret, its User-Agent, and its proxy wiring."""

from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ucode.agents.desktop import vscode
from ucode.cli import app
from ucode.state import save_state

runner = CliRunner()
WS = "https://configured.example"


class TestBridgeSecret:
    def test_created_on_first_use_and_reused(self):
        first = vscode.bridge_secret()

        assert len(first) >= 32
        assert vscode.bridge_secret() == first
        assert json.loads(vscode.BRIDGE_STATE_PATH.read_text(encoding="utf-8")) == {"secret": first}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_file_is_private_to_the_user(self):
        vscode.bridge_secret()

        assert vscode.BRIDGE_STATE_PATH.stat().st_mode & 0o777 == 0o600

    def test_rotate_replaces_the_secret(self):
        first = vscode.bridge_secret()

        rotated = vscode.rotate_bridge_secret()

        assert rotated != first
        assert vscode.bridge_secret() == rotated

    @pytest.mark.parametrize("content", ["not json", '{"secret": ""}', '{"secret": 7}', "[]"])
    def test_unusable_file_gets_a_fresh_secret(self, content):
        vscode.BRIDGE_STATE_PATH.write_text(content, encoding="utf-8")

        secret = vscode.bridge_secret()

        assert secret
        assert json.loads(vscode.BRIDGE_STATE_PATH.read_text(encoding="utf-8")) == {
            "secret": secret
        }


class TestBridgeUserAgent:
    def test_attributes_copilot_chat_with_its_version(self):
        user_agent = vscode.bridge_user_agent("1.2.3")("GitHubCopilotChat/0.71.0")

        assert re.fullmatch(r"ucode/1\.2\.3 githubcopilotchat/0\.71\.0 \([^)]+\)", user_agent)

    def test_finds_the_product_among_other_tokens(self):
        user_agent = vscode.bridge_user_agent("1.2.3")("node-fetch GitHubCopilotChat/0.72.1-rc")

        assert " githubcopilotchat/0.72.1-rc " in user_agent

    @pytest.mark.parametrize("client", [None, "", "OpenAI/JS 5.20.1"])
    def test_unknown_client_is_still_attributed_to_copilot_chat(self, client):
        user_agent = vscode.bridge_user_agent("1.2.3")(client)

        assert re.fullmatch(r"ucode/1\.2\.3 githubcopilotchat \([^)]+\)", user_agent)


class TestVscodeBridgeCommand:
    def test_serves_the_configured_workspace_on_the_requested_port(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        save_state(
            {
                "workspace": WS,
                "profile": "saved-profile",
                "copilot_http_headers": {"X-Team": "data", "Authorization": "ignored"},
            }
        )
        save_state({"workspace": "https://other.example", "profile": "other-profile"})
        server, cache, client = MagicMock(), MagicMock(), MagicMock()
        with (
            patch(
                "ucode.gateway_proxy.start_gateway_auth_proxy",
                return_value=(server, cache, client),
            ) as start,
            patch("ucode.custom_oauth.get_databricks_token", return_value="fresh-token") as token,
        ):
            result = runner.invoke(app, ["vscode-bridge", "--host", WS, "--port", "43200"])
            assert start.call_args.args[1](True) == "fresh-token"

        assert result.exit_code == 0, result.output
        token.assert_called_once_with(WS, "saved-profile", force_refresh=True)
        assert start.call_args.args[0] == WS
        kwargs = start.call_args.kwargs
        assert kwargs["port"] == 43200
        assert kwargs["session_secret"] == vscode.bridge_secret()
        assert kwargs["added_headers"] == {"X-Team": "data"}
        assert kwargs["dropped_chat_completions_fields"] == ("reasoning_effort",)
        assert kwargs["user_agent"]("GitHubCopilotChat/0.71.0").startswith("ucode/")
        assert WS in kwargs["sign_in_hint"]
        server.serve_forever.assert_called_once()

    def test_unconfigured_workspace_is_an_actionable_error(self):
        with patch("ucode.gateway_proxy.start_gateway_auth_proxy") as start:
            result = runner.invoke(app, ["vscode-bridge", "--host", WS, "--port", "43200"])

        assert result.exit_code == 1
        assert "Run `ug configure` first" in " ".join(result.output.split())
        start.assert_not_called()

    def test_busy_port_is_an_error(self):
        save_state({"workspace": WS, "profile": "saved-profile"})
        with (
            patch(
                "ucode.gateway_proxy.start_gateway_auth_proxy",
                side_effect=OSError("Address already in use"),
            ),
            patch("ucode.custom_oauth.get_databricks_token", return_value="fresh-token"),
        ):
            result = runner.invoke(app, ["vscode-bridge", "--host", WS, "--port", "43200"])

        assert result.exit_code == 1
        assert "Address already in use" in result.output

    def test_refresh_failures_go_to_the_bridge_log(self):
        vscode._log_refresh_failure(RuntimeError("refresh token expired"))

        assert "token refresh failed: refresh token expired" in vscode.BRIDGE_LOG_PATH.read_text(
            encoding="utf-8"
        )
