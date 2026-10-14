"""Tests for the hidden custom-client OAuth feature."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

import ucode.cli as cli_mod
import ucode.custom_oauth as oauth_mod
from ucode.cli import app
from ucode.custom_oauth import (
    build_custom_auth_token_argv,
    get_custom_client_token,
)
from ucode.smart_routing import v2

WS = "https://example.databricks.com"
TEST_SCOPES = ("offline_access", "catalog.catalogs:read")
runner = CliRunner()


class TestCustomClientToken:
    def test_smart_routing_uses_saved_custom_profile_without_flag(self, monkeypatch):
        monkeypatch.delenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", raising=False)
        fetch = Mock(return_value="custom-token")
        monkeypatch.setattr(oauth_mod, "get_databricks_token", fetch)
        config = oauth_mod.create_custom_oauth_config("custom-client", TEST_SCOPES)
        config["profile"] = "custom-profile"

        assert (
            v2._launch_token({"custom_oauth": config, "profile": "other-profile"}, WS)
            == "custom-token"
        )
        fetch.assert_called_once_with(WS, "custom-profile", force_refresh=False)

    @pytest.mark.parametrize("flag", [None, "0", "1"])
    @pytest.mark.parametrize("profile", [None, "custom-profile"])
    def test_uses_cli_profile_regardless_of_retired_flag(self, monkeypatch, flag, profile):
        if flag is None:
            monkeypatch.delenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", raising=False)
        else:
            monkeypatch.setenv("ENABLE_CUSTOM_OAUTH_FROM_CLI", flag)
        fetch = Mock(return_value="custom-token")
        monkeypatch.setattr(oauth_mod, "get_databricks_token", fetch)

        assert (
            get_custom_client_token(
                WS + "/", "custom-client", scopes=TEST_SCOPES, profile=profile, force_refresh=True
            )
            == "custom-token"
        )
        fetch.assert_called_once_with(
            WS, profile or "ug-oauth-example.databricks.com-custom-client", force_refresh=True
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"client_id": " "}, "--client-id must not be empty"),
            ({"redirect_url": "https://example.com/callback"}, "--redirect-url must be"),
            ({"scopes": ["offline_access"]}, "API OAuth scope"),
            ({"scopes": ["all-apis"]}, "OAuth scopes must include offline_access"),
            ({"scopes": "offline_access,all-apis"}, "sequence of scope names"),
        ],
    )
    def test_rejects_invalid_options_before_token_fetch(self, monkeypatch, options, message):
        fetch = Mock()
        monkeypatch.setattr(oauth_mod, "get_databricks_token", fetch)
        with pytest.raises(RuntimeError, match=message):
            get_custom_client_token(
                WS, **({"client_id": "custom-client", "scopes": TEST_SCOPES} | options)
            )
        fetch.assert_not_called()

    def test_saved_cli_profile_helper_omits_setup_options(self, monkeypatch):
        monkeypatch.setattr("ucode.databricks.ug_binary", lambda: "/tools/ug")

        assert build_custom_auth_token_argv(
            WS,
            {
                "client_id": "custom-client",
                "redirect_url": "http://localhost:8020/callback",
                "scopes": ["offline_access", "model-serving"],
                "profile": "custom-profile",
            },
        ) == [
            "/tools/ug",
            "auth-token",
            "--host",
            WS,
            "--profile",
            "custom-profile",
        ]


class TestCustomOAuthCLISetup:
    @pytest.mark.parametrize("authenticated", [False, True])
    def test_creates_profile_and_logs_in_only_when_needed(self, monkeypatch, authenticated):
        config = oauth_mod.create_custom_oauth_config("custom-client", TEST_SCOPES)
        version = Mock()
        login = Mock()
        token = Mock(return_value="custom-token")
        monkeypatch.setattr(oauth_mod, "ensure_databricks_cli_version", version)
        monkeypatch.setattr(oauth_mod, "external_bearer_configured", lambda: False)
        auth = Mock(return_value=authenticated)
        monkeypatch.setattr(oauth_mod, "has_valid_databricks_auth", auth)
        monkeypatch.setattr(oauth_mod, "databricks_cli_path", lambda: "databricks")
        monkeypatch.setattr(oauth_mod, "run", login)
        monkeypatch.setattr(oauth_mod, "get_databricks_token", token)

        assert oauth_mod.ensure_custom_oauth_cli_token(WS, config) == "custom-token"
        profile = "ug-oauth-example.databricks.com-custom-client"
        assert config["profile"] == profile
        version.assert_called_once_with((1, 17, 0))
        auth.assert_called_once_with(WS, profile)
        token.assert_called_once_with(WS, profile)
        if authenticated:
            login.assert_not_called()
        else:
            login.assert_called_once_with(
                [
                    "databricks",
                    "auth",
                    "login",
                    "--host",
                    WS,
                    "--profile",
                    profile,
                    "--client-id",
                    "custom-client",
                    "--scopes",
                    "catalog.catalogs:read",
                ],
                timeout=180,
            )


class TestCustomClientCommand:
    @pytest.fixture(autouse=True)
    def _no_production_auth(self, monkeypatch):
        monkeypatch.setattr(
            "ucode.cli.get_databricks_token",
            Mock(side_effect=AssertionError("Custom auth must not use production auth")),
        )

    def test_client_id_alone_uses_default_scopes(self):
        with (
            patch("ucode.cli.load_state", return_value={"workspace": WS}),
            patch(
                "ucode.custom_oauth.get_custom_client_token", return_value="custom-token"
            ) as fetch,
        ):
            result = runner.invoke(app, ["auth-token", "--client-id", "my-client"])
        assert result.exit_code == 0, result.output
        assert result.stdout == "custom-token\n"
        assert fetch.call_args.kwargs["scopes"] == oauth_mod.DEFAULT_CLI_SCOPES

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
            profile="saved",
            force_refresh=True,
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            (["--client-id", "my-client", "--use-pat"], "cannot be combined"),
            (["--redirect-url", "http://localhost:8020"], "requires --client-id"),
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
            patch("ucode.cli.refresh_managed_config", return_value=(None, False)),
            patch("ucode.cli.configure_single_tool", return_value=state),
            patch("ucode.cli.install_databricks_ai_tools_for_agents"),
        ):
            result = cli_mod.configure_workspace_command(
                "claude",
                workspaces=[(WS, None)],
            )

        assert result == 0
        assert shared.call_args.kwargs["clear_custom_oauth"] is True

    @pytest.mark.parametrize("saved", [False, True])
    def test_shared_state_persists_custom_oauth(self, monkeypatch, saved):
        custom_oauth = {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "model-serving"],
        }
        saved_states = []
        monkeypatch.setattr(
            cli_mod,
            "load_state",
            lambda: {"workspace": WS, **({"custom_oauth": custom_oauth} if saved else {})},
        )
        monkeypatch.setattr(cli_mod, "save_state", lambda state: saved_states.append(dict(state)))
        monkeypatch.setattr(cli_mod, "find_profile_name_for_host", lambda _workspace: None)
        authenticate = Mock(return_value="custom-token")
        monkeypatch.setattr(cli_mod, "ensure_custom_oauth_cli_token", authenticate)

        state = cli_mod.configure_shared_state(
            WS,
            tools=["claude"],
            skip_preflight=True,
            custom_oauth=None if saved else custom_oauth,
        )

        assert state["custom_oauth"] == custom_oauth
        assert saved_states[-1]["custom_oauth"] == custom_oauth
        authenticate.assert_called_once_with(WS, custom_oauth)

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
                ],
            )

        assert result.exit_code == 0, result.output
        assert launch.call_args.kwargs["custom_oauth"] == {
            "client_id": "custom-client",
            "redirect_url": "http://localhost:8020/callback",
            "scopes": ["offline_access", "all-apis"],
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
