"""Tests for `ug mcp login` (mcp_login): status classification via the existing
UC REST APIs, the `--resource` login invocation, and command orchestration.

Network-free: the UC REST calls (`_http_get_json`), the current-user lookup, and
the CLI subprocess are monkeypatched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ucode import mcp_login

WS = "https://ws.staging.cloud.databricks.com"
FULL = "system.ai.github"
URL = f"{WS}/ai-gateway/mcp-services/{FULL}"
USER = "user@databricks.com"

_DETAILS = {
    "id": "svc-123",
    "config": {
        "source_connection": {
            "name": "connections/github",
            "securable_kind": "CONNECTION_HTTP_OAUTH_U2M_MAPPING",
        }
    },
}


def _fake_http(details=_DETAILS, details_err=None, cred=None, cred_err=None):
    """Return an `_http_get_json` stub that answers the mcp-services details call
    and the user-credentials call based on the URL."""

    def _http(url, token, *args, **kwargs):
        if "/mcp-services/" in url:
            return details, details_err
        if "/user-credentials/" in url:
            return cred, cred_err
        return None, "unexpected url"

    return _http


class TestFullNameFromUrl:
    def test_extracts_service_name(self):
        assert mcp_login.mcp_service_full_name_from_url(URL) == FULL

    def test_ignores_query_and_trailing(self):
        assert mcp_login.mcp_service_full_name_from_url(f"{URL}/?x=1") == FULL

    def test_non_mcp_service_url_is_none(self):
        assert mcp_login.mcp_service_full_name_from_url(f"{WS}/api/2.0/mcp/external/foo") is None


class TestLoginStatus:
    def test_authenticated_when_credential_active(self, monkeypatch):
        cred = {"connection_user_credential": {"provisioning_info": {"state": "ACTIVE"}}}
        monkeypatch.setattr(mcp_login, "_http_get_json", _fake_http(cred=cred))
        assert (
            mcp_login.mcp_service_login_status(WS, "t", FULL, USER)
            == mcp_login.STATUS_AUTHENTICATED
        )

    def test_needs_login_on_404_not_found(self, monkeypatch):
        # The user-credentials endpoint answers 404 when there is no credential yet.
        monkeypatch.setattr(mcp_login, "_http_get_json", _fake_http(cred_err="HTTP 404 Not Found"))
        assert (
            mcp_login.mcp_service_login_status(WS, "t", FULL, USER) == mcp_login.STATUS_NEEDS_LOGIN
        )

    def test_needs_login_when_state_not_active(self, monkeypatch):
        cred = {"connection_user_credential": {"provisioning_info": {"state": "PROVISIONING"}}}
        monkeypatch.setattr(mcp_login, "_http_get_json", _fake_http(cred=cred))
        assert (
            mcp_login.mcp_service_login_status(WS, "t", FULL, USER) == mcp_login.STATUS_NEEDS_LOGIN
        )

    def test_no_login_needed_for_non_oauth_kind(self, monkeypatch):
        details = {
            "id": "x",
            "config": {
                "source_connection": {"name": "connections/c", "securable_kind": "CONNECTION_MYSQL"}
            },
        }
        monkeypatch.setattr(mcp_login, "_http_get_json", _fake_http(details=details))
        assert mcp_login.mcp_service_login_status(WS, "t", FULL, USER) == mcp_login.STATUS_NO_LOGIN

    def test_unknown_when_details_error(self, monkeypatch):
        monkeypatch.setattr(
            mcp_login, "_http_get_json", _fake_http(details=None, details_err="HTTP 500")
        )
        assert mcp_login.mcp_service_login_status(WS, "t", FULL, USER) == mcp_login.STATUS_UNKNOWN

    def test_unknown_when_credential_error_not_404(self, monkeypatch):
        monkeypatch.setattr(mcp_login, "_http_get_json", _fake_http(cred_err="HTTP 403 Forbidden"))
        assert mcp_login.mcp_service_login_status(WS, "t", FULL, USER) == mcp_login.STATUS_UNKNOWN


class TestRunConnectionLogin:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            mcp_login.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=0, stderr="", stdout=""),
        )
        ok, _ = mcp_login.run_connection_login(URL, WS, "p")
        assert ok is True

    def test_old_cli_without_resource_flag_gives_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            mcp_login.subprocess,
            "run",
            lambda *a, **k: MagicMock(returncode=1, stderr="unknown flag: --resource", stdout=""),
        )
        ok, detail = mcp_login.run_connection_login(URL, WS, "p")
        assert ok is False
        assert "does not support `--resource`" in detail

    def test_missing_binary(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError()

        monkeypatch.setattr(mcp_login.subprocess, "run", boom)
        ok, detail = mcp_login.run_connection_login(URL, WS, "p")
        assert ok is False and "not found" in detail

    def test_passes_host_and_resource(self, monkeypatch):
        seen = {}

        def capture(cmd, *a, **k):
            seen["cmd"] = cmd
            return MagicMock(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(mcp_login.subprocess, "run", capture)
        mcp_login.run_connection_login(URL, WS, "prof")
        assert seen["cmd"][:3] == ["databricks", "auth", "login"]
        assert "--host" in seen["cmd"] and WS in seen["cmd"]
        assert "--resource" in seen["cmd"] and URL in seen["cmd"]
        assert seen["cmd"][-2:] == ["--profile", "prof"]


class TestLoginCommand:
    def _state(self):
        return {
            "workspace": WS,
            "profile": "p",
            "mcp_servers": [
                {"name": "system-ai-github", "url": URL, "clients": ["claude", "codex"]},
                {
                    "name": "databricks-skill-registry",
                    "url": f"{WS}/ai-gateway/skills/x",
                    "clients": ["claude"],
                },
            ],
        }

    def test_services_targets_only_named_and_logs_in(self, monkeypatch):
        monkeypatch.setattr(mcp_login, "load_state", lambda: self._state())
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"userName": USER})
        monkeypatch.setattr(
            mcp_login,
            "mcp_service_login_status",
            lambda ws, t, full, u: mcp_login.STATUS_NEEDS_LOGIN,
        )
        logged: list[str] = []
        monkeypatch.setattr(
            mcp_login,
            "run_connection_login",
            lambda url, ws, p=None, **k: logged.append(url) or (True, "ok"),
        )
        rc = mcp_login.login_mcp_command(services={"github"})
        assert rc == 0
        assert logged == [
            URL
        ]  # matched by short name; skills entry ignored (not a connection mcp-service)

    def test_agents_scope_excludes_unconfigured_agent(self, monkeypatch):
        monkeypatch.setattr(mcp_login, "load_state", lambda: self._state())
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"userName": USER})
        monkeypatch.setattr(
            mcp_login, "mcp_service_login_status", lambda *a: mcp_login.STATUS_NEEDS_LOGIN
        )
        logged: list[str] = []
        monkeypatch.setattr(
            mcp_login,
            "run_connection_login",
            lambda url, ws, p=None, **k: logged.append(url) or (True, "ok"),
        )
        # gemini isn't a client of the github service → nothing to do
        rc = mcp_login.login_mcp_command(services={"github"}, agents={"gemini"})
        assert rc == 0 and logged == []

    def test_no_configured_services_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            mcp_login, "load_state", lambda: {"workspace": WS, "profile": "p", "mcp_servers": []}
        )
        rc = mcp_login.login_mcp_command()
        assert rc == 0
