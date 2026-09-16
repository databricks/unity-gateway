"""Tests for the hidden custom-client OAuth feature."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

import ucode.cli as cli_mod
import ucode.custom_oauth as oauth_mod
import ucode.databricks as db_mod
from ucode.cli import app
from ucode.custom_oauth import get_custom_client_token

WS = "https://example.databricks.com"
TEST_SCOPES = ("offline_access", "catalog.catalogs:read")
runner = CliRunner()


class TestCustomClientToken:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db_mod, "databricks_cli_version", lambda: (1, 17, 0))
        monkeypatch.setattr(oauth_mod, "CUSTOM_OAUTH_CONFIG_FILE", tmp_path / "oauth.cfg")

    @staticmethod
    def _result(returncode=0, stdout=""):
        return subprocess.CompletedProcess([], returncode, stdout, "")

    def test_reuses_cli_token_without_login(self, monkeypatch):
        run = Mock(return_value=self._result(stdout='{"access_token":"cached"}'))
        monkeypatch.setattr(oauth_mod.subprocess, "run", run)

        assert get_custom_client_token(WS + "/", "custom-client", scopes=TEST_SCOPES) == "cached"
        command = run.call_args.args[0]
        assert command[:3] == ["databricks", "auth", "token"]
        assert run.call_args.kwargs["env"]["DATABRICKS_CONFIG_FILE"].endswith("oauth.cfg")

    def test_login_then_retries_token(self, monkeypatch):
        run = Mock(
            side_effect=[
                self._result(1),
                self._result(),
                self._result(stdout='{"access_token":"new-token"}'),
            ]
        )
        monkeypatch.setattr(oauth_mod.subprocess, "run", run)

        assert get_custom_client_token(WS, "custom-client", scopes=TEST_SCOPES) == "new-token"
        first, login, retry = [call.args[0] for call in run.call_args_list]
        assert first[first.index("--profile") + 1] == retry[retry.index("--profile") + 1]
        assert login[:3] == ["databricks", "auth", "login"]
        assert login[login.index("--client-id") + 1] == "custom-client"
        assert login[login.index("--scopes") + 1] == ",".join(TEST_SCOPES)
        assert login[login.index("--timeout") + 1] == "3m"
        assert login[login.index("--profile") + 1] == first[first.index("--profile") + 1]
        assert login[login.index("--profile") + 1].startswith("ug-custom-oauth-")
        assert run.call_args_list[1].kwargs["capture_output"] is True

    def test_force_refresh_is_forwarded(self, monkeypatch):
        run = Mock(return_value=self._result(stdout='{"access_token":"fresh"}'))
        monkeypatch.setattr(oauth_mod.subprocess, "run", run)
        assert (
            get_custom_client_token(WS, "custom-client", scopes=TEST_SCOPES, force_refresh=True)
            == "fresh"
        )
        assert "--force-refresh" in run.call_args.args[0]

    def test_invalid_redirect_fails_before_network(self):
        with pytest.raises(RuntimeError, match="--redirect-url must be"):
            get_custom_client_token(
                WS,
                client_id="custom-client",
                redirect_url="https://example.com/callback",
                scopes=TEST_SCOPES,
            )

    def test_old_cli_is_actionable(self, monkeypatch):
        monkeypatch.setattr(db_mod, "databricks_cli_version", lambda: (1, 16, 1))
        with pytest.raises(RuntimeError, match="v1.17.0 or newer"):
            get_custom_client_token(WS, "custom-client", scopes=TEST_SCOPES)

    def test_login_failure_is_actionable_and_sanitized(self, monkeypatch):
        run = Mock(side_effect=[self._result(1), self._result(1, "sensitive response")])
        monkeypatch.setattr(oauth_mod.subprocess, "run", run)
        with pytest.raises(RuntimeError, match="returned no custom-client OAuth token") as error:
            get_custom_client_token(WS, "custom-client", scopes=TEST_SCOPES)
        assert "sensitive server response" not in str(error.value)

    @pytest.mark.parametrize("scopes", [["offline_access"], ["catalog.catalogs:read"]])
    def test_api_scopes_are_required(self, scopes):
        with pytest.raises(RuntimeError, match="OAuth scopes|API OAuth scope"):
            get_custom_client_token(WS, client_id="custom-client", scopes=scopes)


class TestCustomClientCommand:
    @pytest.fixture(autouse=True)
    def _no_production_auth(self, monkeypatch):
        monkeypatch.setattr(
            "ucode.cli.get_databricks_token",
            Mock(side_effect=AssertionError("Custom auth must not use production auth")),
        )

    def test_custom_client_dispatches_with_explicit_scopes(self):
        with (
            patch(
                "ucode.cli.load_state",
                return_value={"workspace": "https://ws", "profile": "saved", "use_pat": True},
            ),
            patch("ucode.cli.ensure_pat_bearer") as pat,
            patch(
                "ucode.custom_oauth.get_custom_client_token", return_value="custom-token"
            ) as fetch,
        ):
            result = runner.invoke(
                app,
                [
                    "auth-token",
                    "--client-id",
                    "my-client",
                    "--redirect-url",
                    "http://localhost:41735/callback",
                    "--scopes",
                    "offline_access,catalog.catalogs:read",
                    "--force-refresh",
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.stdout == "custom-token\n"
        pat.assert_not_called()
        fetch.assert_called_once_with(
            "https://ws",
            client_id="my-client",
            redirect_url="http://localhost:41735/callback",
            scopes=["offline_access", "catalog.catalogs:read"],
            force_refresh=True,
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            (["--client-id", "my-client", "--use-pat"], "cannot be combined"),
            (["--redirect-url", "http://localhost:8020"], "requires --client-id"),
            (["--client-id", "my-client"], "--scopes is required"),
        ],
    )
    def test_invalid_custom_client_options(self, options, message):
        with patch("ucode.custom_oauth.get_custom_client_token") as fetch:
            result = runner.invoke(app, ["auth-token", *options])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert message in result.stderr
        fetch.assert_not_called()


class TestConfigureCustomOAuth:
    def test_forwards_config_to_claude_configuration(self):
        with (
            patch("ucode.cli.install_databricks_cli"),
            patch("ucode.cli.install_tool_binary"),
            patch("ucode.cli.configure_workspace_command") as configure_workspace,
        ):
            result = runner.invoke(
                app,
                [
                    "configure",
                    "--agent",
                    "claude",
                    "--workspace",
                    WS,
                    "--client-id",
                    "custom-client",
                    "--redirect-url",
                    "http://localhost:8020/callback",
                    "--scopes",
                    "offline_access,model-serving",
                ],
            )
        assert result.exit_code == 0, result.output
        configure_workspace.assert_called_once_with(
            "claude",
            workspaces=[(WS, None)],
            custom_oauth={
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
            },
        )

    def test_configure_without_custom_options_resets_custom_oauth(self):
        state = {"workspace": WS, "available_tools": ["claude"]}
        with (
            patch("ucode.cli._configure_shared_workspace_states", return_value=[state]) as shared,
            patch("ucode.cli.configure_single_tool", return_value=state),
            patch("ucode.cli.install_databricks_ai_tools_for_agents"),
        ):
            result = cli_mod.configure_workspace_command(
                "claude",
                workspaces=[(WS, None)],
            )

        assert result == 0
        assert shared.call_args.kwargs["clear_custom_oauth"] is True

    def test_shared_state_persists_custom_oauth(self, monkeypatch):
        custom_oauth = {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "model-serving"],
        }
        saved = []
        monkeypatch.setattr(cli_mod, "load_state", lambda: {"workspace": WS})
        monkeypatch.setattr(cli_mod, "save_state", lambda state: saved.append(dict(state)))
        monkeypatch.setattr(cli_mod, "find_profile_name_for_host", lambda _workspace: None)

        state = cli_mod.configure_shared_state(
            WS,
            tools=["claude"],
            skip_preflight=True,
            custom_oauth=custom_oauth,
        )

        assert state["custom_oauth"] == custom_oauth
        assert saved[-1]["custom_oauth"] == custom_oauth

    def test_shared_state_clears_custom_oauth(self, monkeypatch):
        monkeypatch.setattr(
            cli_mod,
            "load_state",
            lambda: {"workspace": WS, "custom_oauth": {"client_id": "old"}},
        )
        monkeypatch.setattr(cli_mod, "save_state", lambda _state: None)
        monkeypatch.setattr(cli_mod, "find_profile_name_for_host", lambda _workspace: None)

        state = cli_mod.configure_shared_state(
            WS,
            tools=["claude"],
            skip_preflight=True,
            clear_custom_oauth=True,
        )

        assert "custom_oauth" not in state


class TestLaunchCustomOAuth:
    @pytest.mark.parametrize("command", ["auth-token", "configure", "claude", "codex"])
    def test_options_are_hidden(self, command):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--client-id" not in result.output
        assert "--redirect-url" not in result.output
        assert "--scopes" not in result.output

    @pytest.mark.parametrize("tool", ["claude", "codex"])
    def test_launch_forwards_custom_oauth(self, tool):
        with patch("ucode.cli._launch_tool") as launch:
            result = runner.invoke(
                app,
                [
                    tool,
                    "--workspace",
                    WS,
                    "--client-id",
                    "custom-client",
                    "--redirect-url",
                    "http://localhost:8020/callback",
                    "--scopes",
                    "offline_access,model-serving",
                ],
            )

        assert result.exit_code == 0, result.output
        assert launch.call_args.kwargs["custom_oauth"] == {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "model-serving"],
        }

    def test_auto_configure_receives_custom_oauth(self):
        custom_oauth = {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "model-serving"],
        }
        state = {"workspace": WS, "profile": None, "available_tools": ["codex"]}
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli.configure_shared_state", return_value=state) as configure_shared,
            patch("ucode.cli.configure_single_tool", return_value=state),
        ):
            cli_mod._auto_configure_tool("codex", custom_oauth=custom_oauth)

        configure_shared.assert_called_once_with(
            WS,
            profile=None,
            tools=["codex"],
            custom_oauth=custom_oauth,
        )

    def test_auto_configure_does_not_clear_custom_oauth(self):
        state = {"workspace": WS, "profile": None, "available_tools": ["claude"]}
        with (
            patch("ucode.cli.load_state", return_value=state),
            patch("ucode.cli.configure_shared_state", return_value=state) as configure_shared,
            patch("ucode.cli.configure_single_tool", return_value=state),
        ):
            cli_mod._auto_configure_tool("claude")

        configure_shared.assert_called_once_with(WS, profile=None, tools=["claude"])
