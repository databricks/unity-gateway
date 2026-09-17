"""Tests for `ug mcp login` (mcp_login): status classification via the existing
UC REST APIs, the `--resource` login invocation, and command orchestration.

Network-free: the UC REST calls (`_http_get_json`), the current-user lookup, and
the CLI subprocess are monkeypatched.
"""

from __future__ import annotations

import pytest

from ucode import mcp, mcp_login

WS = "https://ws.staging.cloud.databricks.com"
FULL = "system.ai.github"
URL = f"{WS}/ai-gateway/mcp-services/{FULL}"
USER = "user@databricks.com"
USER_ID = "1234567890"  # numeric workspace user id (the connection-user-credentials key)

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

    def test_credential_lookup_keys_on_numeric_user_id_not_email(self, monkeypatch):
        # The connection user-credentials API keys on the numeric workspace user id; passing the
        # email 404s and mis-reports an already-signed-in service as `needs sign-in`.
        seen: dict[str, str] = {}

        def _http(url, token, *args, **kwargs):
            if "/mcp-services/" in url:
                return _DETAILS, None
            seen["cred_url"] = url
            return {"connection_user_credential": {"provisioning_info": {"state": "ACTIVE"}}}, None

        monkeypatch.setattr(mcp_login, "_http_get_json", _http)
        status = mcp_login.mcp_service_login_status(WS, "t", FULL, USER_ID)
        assert status == mcp_login.STATUS_AUTHENTICATED
        assert f"/user-credentials/{USER_ID}?" in seen["cred_url"]
        assert "user%40" not in seen["cred_url"]  # the email was not used as the key


class TestLoginCommand:
    @pytest.fixture(autouse=True)
    def _no_managed_files(self, monkeypatch):
        # `configured_mcp_servers_by_name` reads the agents' OS-managed files; stub them empty so
        # these state-driven cases don't pick up managed servers from the host.
        monkeypatch.setattr(mcp.claude, "read_managed_mcp_urls", dict)
        monkeypatch.setattr(mcp.codex, "read_managed_mcp_urls", dict)

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
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
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
        rc = mcp_login.login_mcp_command(names={"github"})
        assert rc == 0
        assert logged == [
            URL
        ]  # matched by short name; skills entry ignored (not a connection mcp-service)

    def test_agents_scope_excludes_unconfigured_agent(self, monkeypatch):
        monkeypatch.setattr(mcp_login, "load_state", lambda: self._state())
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
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
        rc = mcp_login.login_mcp_command(names={"github"}, agents={"gemini"})
        assert rc == 0 and logged == []

    def test_no_configured_services_is_noop(self, monkeypatch):
        monkeypatch.setattr(
            mcp_login, "load_state", lambda: {"workspace": WS, "profile": "p", "mcp_servers": []}
        )
        rc = mcp_login.login_mcp_command()
        assert rc == 0

    def test_workspace_managed_service_is_included(self, monkeypatch):
        # Uses the shared enumeration, so workspace-managed mcp-services are covered too.
        slack_url = f"{WS}/ai-gateway/mcp-services/system.ai.slack"
        state = {
            "workspace": WS,
            "profile": "p",
            "mcp_servers": [],
            "managed_mcp_servers": [
                {"name": "system-ai-slack", "url": slack_url, "clients": ["claude"]}
            ],
        }
        monkeypatch.setattr(mcp_login, "load_state", lambda: state)
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
        monkeypatch.setattr(
            mcp_login, "mcp_service_login_status", lambda *a: mcp_login.STATUS_NEEDS_LOGIN
        )
        logged: list[str] = []
        monkeypatch.setattr(
            mcp_login,
            "run_connection_login",
            lambda url, ws, p=None, **k: logged.append(url) or (True, "ok"),
        )
        rc = mcp_login.login_mcp_command(names={"slack"})
        assert rc == 0 and logged == [slack_url]

    def test_os_managed_file_service_is_included(self, monkeypatch):
        # A managed mcp-service delivered to an agent's OS-managed file (not state) is still seen by
        # `ug mcp login` — otherwise you couldn't pre-sign-in to managed servers.
        gh_url = f"{WS}/ai-gateway/mcp-services/system.ai.github"
        state = {"workspace": WS, "profile": "p", "mcp_servers": [], "managed_mcp_servers": []}
        monkeypatch.setattr(mcp_login, "load_state", lambda: state)
        monkeypatch.setattr(
            mcp.claude, "read_managed_mcp_urls", lambda: {"system-ai-github": gh_url}
        )
        monkeypatch.setattr(mcp.codex, "read_managed_mcp_urls", dict)
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
        monkeypatch.setattr(
            mcp_login, "mcp_service_login_status", lambda *a: mcp_login.STATUS_NEEDS_LOGIN
        )
        logged: list[str] = []
        monkeypatch.setattr(
            mcp_login,
            "run_connection_login",
            lambda url, ws, p=None, **k: logged.append(url) or (True, "ok"),
        )
        rc = mcp_login.login_mcp_command(names={"github"})
        assert rc == 0 and logged == [gh_url]

    def _setup(self, monkeypatch, status, *, logged, selection_capture=None):
        """Wire load_state/token/user + a fixed status for every service, capturing sign-ins.
        `status` is a str (same for all) or a {full_name: status} map."""
        monkeypatch.setattr(mcp_login, "load_state", lambda: self._state())
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
        resolve = (lambda full: status[full]) if isinstance(status, dict) else (lambda full: status)
        monkeypatch.setattr(
            mcp_login, "mcp_service_login_status", lambda ws, t, full, u: resolve(full)
        )
        monkeypatch.setattr(
            mcp_login,
            "run_connection_login",
            lambda url, ws, p=None, **k: logged.append(url) or (True, "ok"),
        )
        if selection_capture is not None:
            monkeypatch.setattr(
                mcp_login,
                "_prompt_login_selection",
                lambda rows: selection_capture.extend(rows) or [],
            )

    def test_services_skips_already_signed_in(self, monkeypatch):
        # `ug mcp login --names github` must NOT re-run the browser login for an already-signed-in
        # service; only NEEDS_LOGIN/UNKNOWN are actionable.
        logged: list[str] = []
        self._setup(monkeypatch, mcp_login.STATUS_AUTHENTICATED, logged=logged)
        rc = mcp_login.login_mcp_command(names={"github"})
        assert rc == 0 and logged == []

    def test_services_signs_in_unknown_status(self, monkeypatch):
        # An UNKNOWN status (e.g. UC API unreachable) is still actionable, so the non-interactive
        # path attempts the sign-in rather than silently skipping it.
        logged: list[str] = []
        self._setup(monkeypatch, mcp_login.STATUS_UNKNOWN, logged=logged)
        rc = mcp_login.login_mcp_command(names={"github"})
        assert rc == 0 and logged == [URL]

    def test_picker_excludes_no_login_services(self, monkeypatch):
        # A NO_LOGIN service (its connection needs no per-user login) must not reach the picker,
        # where it would show pre-checked and trigger a pointless sign-in.
        state = {
            "workspace": WS,
            "profile": "p",
            "mcp_servers": [
                {"name": "system-ai-github", "url": URL, "clients": ["claude"]},
                {
                    "name": "system-ai-pg",
                    "url": f"{WS}/ai-gateway/mcp-services/system.ai.pg",
                    "clients": ["claude"],
                },
            ],
        }
        monkeypatch.setattr(mcp_login, "load_state", lambda: state)
        monkeypatch.setattr(mcp_login, "get_databricks_token", lambda ws, p: "tok")
        monkeypatch.setattr(mcp_login, "_scim_me", lambda ws, t: {"id": USER_ID, "userName": USER})
        monkeypatch.setattr(
            mcp_login,
            "mcp_service_login_status",
            lambda ws, t, full, u: mcp_login.STATUS_NO_LOGIN
            if full == "system.ai.pg"
            else mcp_login.STATUS_NEEDS_LOGIN,
        )
        rows: list = []
        monkeypatch.setattr(
            mcp_login, "_prompt_login_selection", lambda r: rows.extend(r) or []
        )
        rc = mcp_login.login_mcp_command()
        assert rc == 0
        offered = {full for full, _status in rows}
        assert offered == {"system.ai.github"}  # the NO_LOGIN service is not offered

    def test_unknown_status_is_not_reported_as_all_signed_in(self, monkeypatch):
        # If every status is UNKNOWN, the command must still show the picker, not claim everything
        # is already signed in and skip it.
        captured: list = []
        logged: list[str] = []
        self._setup(
            monkeypatch, mcp_login.STATUS_UNKNOWN, logged=logged, selection_capture=captured
        )
        rc = mcp_login.login_mcp_command()  # interactive (no --names)
        assert rc == 0
        assert [full for full, _status in captured] == ["system.ai.github"]  # picker was shown
