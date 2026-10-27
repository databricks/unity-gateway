"""Tests for `ug vscode-bridge`: its secret, its User-Agent, and its proxy wiring."""

from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ucode import managed_config
from ucode.agents.desktop import vscode
from ucode.cli import app
from ucode.state import clear_state, save_state

runner = CliRunner()
WS = "https://configured.example"


def _install_copilot_policy(http_headers: dict[str, str]) -> None:
    """Install the workspace policy the way `ug configure` leaves it on disk."""
    config = {
        "spec_version": 1,
        "enabled_agents": [
            {"agent": "CODING_AGENT_COPILOT", "config": {"http_headers": http_headers}}
        ],
    }
    managed_config.MANAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    managed_config.MANAGED_CONFIG_PATH.write_text(
        json.dumps({"workspace": WS, "config": config}), encoding="utf-8"
    )


def _start_bridge():
    """Run `ug vscode-bridge` with the proxy start mocked; return the proxy kwargs and cache."""
    server, cache, client = MagicMock(), MagicMock(), MagicMock()
    with patch(
        "ucode.gateway_proxy.start_gateway_auth_proxy", return_value=(server, cache, client)
    ) as start:
        result = runner.invoke(app, ["vscode-bridge", "--host", WS, "--port", "43200"])
    assert result.exit_code == 0, result.output
    server.serve_forever.assert_called_once()
    return start.call_args, cache


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

    def test_unreadable_file_gets_a_new_secret(self):
        vscode.BRIDGE_STATE_PATH.write_text("not json", encoding="utf-8")

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
        save_state({"workspace": WS, "profile": "saved-profile"})
        save_state({"workspace": "https://other.example", "profile": "other-profile"})
        with patch("ucode.custom_oauth.get_databricks_token", return_value="fresh-token") as token:
            call, _cache = _start_bridge()
            assert call.args[1](True) == "fresh-token"

        token.assert_called_once_with(WS, "saved-profile", force_refresh=True)
        assert call.args[0] == WS
        kwargs = call.kwargs
        assert kwargs["port"] == 43200
        assert kwargs["session_secret"] == vscode.bridge_secret()
        assert kwargs["headers_provider"]() == {}
        assert kwargs["dropped_chat_completions_fields"] == ("reasoning_effort",)
        assert kwargs["user_agent"]("GitHubCopilotChat/0.71.0").startswith("ucode/")
        assert WS in kwargs["sign_in_hint"]

    def test_forwards_the_admin_headers_from_the_installed_policy(self, monkeypatch):
        # A launch layers the policy over local state and save_state strips it again, so the
        # headers never reach state.json; the bridge must resolve the policy itself.
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        save_state({"workspace": WS, "profile": "saved-profile"})
        _install_copilot_policy({"X-Team": "data", "Authorization": "ignored"})

        call, _cache = _start_bridge()

        assert "copilot_http_headers" not in json.dumps(vscode.state_module.load_full_state())
        assert call.kwargs["headers_provider"]() == {"X-Team": "data"}

    def test_picks_up_a_policy_update_without_a_restart(self, monkeypatch):
        monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)
        save_state({"workspace": WS, "profile": "saved-profile"})
        _install_copilot_policy({"X-Team": "data"})
        call, _cache = _start_bridge()
        headers = call.kwargs["headers_provider"]
        assert headers() == {"X-Team": "data"}

        _install_copilot_policy({"X-Team": "platform", "X-Cost-Center": "42"})

        assert headers() == {"X-Team": "platform", "X-Cost-Center": "42"}

    def test_a_profile_change_mints_a_token_for_the_new_profile(self):
        save_state({"workspace": WS, "profile": "first-profile"})
        with patch("ucode.custom_oauth.get_databricks_token", return_value="t") as token:
            call, cache = _start_bridge()
            save_state({"workspace": WS, "profile": "second-profile"})

            call.kwargs["headers_provider"]()
            call.args[1](True)

        cache.refresh.assert_called_once()
        assert token.call_args.args == (WS, "second-profile")

    def test_keeps_serving_the_last_state_after_the_workspace_is_cleared(self):
        save_state({"workspace": WS, "profile": "saved-profile"})
        with patch("ucode.custom_oauth.get_databricks_token", return_value="t") as token:
            call, cache = _start_bridge()
            clear_state()

            call.args[1](True)

        cache.refresh.assert_not_called()
        assert token.call_args.args == (WS, "saved-profile")

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
