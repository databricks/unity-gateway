"""Browser connector sign-in with network, browser, and clock boundaries controlled."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from urllib import error as urllib_error
from urllib.parse import parse_qs, urlsplit

import pytest

from ucode import databricks as db_mod
from ucode import mcp_connection_login as mcl

WS = "https://ws.staging.cloud.databricks.com"
AIGW_URL = f"{WS}/ai-gateway/mcp-services/system.ai.github"
SERVICE_URL = f"{WS}/api/2.1/unity-catalog/mcp-services/system.ai.github"
ACTIVE = {"provisioning_info": {"state": "ACTIVE"}}
FAILED = {"provisioning_info": {"state": "FAILED"}}


class TestConnectionFromUrl:
    def test_plain_endpoint(self):
        assert mcl.connection_from_url(AIGW_URL) == "system.ai.github"

    def test_with_trailing_path_and_query(self):
        assert mcl.connection_from_url(f"{AIGW_URL}/tools/list?x=1") == "system.ai.github"

    def test_non_aigw_url_is_none(self):
        assert mcl.connection_from_url(f"{WS}/api/2.0/mcp/functions/system/ai") is None

    def test_missing_service_is_none(self):
        assert mcl.connection_from_url(f"{WS}/ai-gateway/mcp-services/") is None


@pytest.fixture
def sign_in(monkeypatch):
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    now = [100.0]
    monkeypatch.setattr(mcl.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(mcl.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(mcl, "_LOGIN_TIMEOUT_SECONDS", 6)
    db_mod._remember_token(db_mod._token_memo_key(WS, "selected"), "selected-user-token", 3600)
    requests = []
    launches = []
    states = [404, ACTIVE]
    metadata = [{}]

    def urlopen(request, **kwargs):
        requests.append(request)
        if request.full_url.endswith("/user-credentials"):
            result = states.pop(0) if len(states) > 1 else states[0]
        else:
            result = metadata[0]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, int):
            raise urllib_error.HTTPError(request.full_url, result, "test error", {}, None)
        body = result if isinstance(result, bytes) else json.dumps(result).encode()
        response = io.BytesIO(body)
        response.headers = {"X-Databricks-Org-Id": "12345"}
        return response

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(db_mod.urllib_request, "urlopen", urlopen)
    monkeypatch.setattr(mcl.subprocess_cross_os, "run", launch)
    return states, requests, launches, metadata


class TestRunConnectionLogin:
    @pytest.mark.parametrize("credential", [ACTIVE, 403, FAILED])
    def test_browser_sign_in_preserves_workspace_profile(
        self, sign_in, monkeypatch, tmp_path, credential
    ):
        states, _, launches, _ = sign_in
        states[:] = [404, credential]
        config = tmp_path / "databrickscfg"
        original = f"# user-owned\n[selected]\nhost = {WS}\nauth_type = databricks-cli\nclient_id = custom\n"
        config.write_text(original)
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))

        ok, _ = mcl.run_connection_login(AIGW_URL, WS, profile="selected")

        assert ok == (credential == ACTIVE)
        assert config.read_text() == original
        assert len(launches) == 1
        assert launches[0][0][0] == sys.executable
        memo = db_mod._memoized_token(db_mod._token_memo_key(WS, "selected"))
        assert memo is not None and memo.token == "selected-user-token"

    def test_opens_workspace_page_then_checks_selected_user(self, sign_in, capsys):
        states, requests, launches, _ = sign_in
        states[:] = [404, FAILED, ACTIVE]

        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected") == (True, "signed in")

        argv, kwargs = launches[0]
        assert argv[0] == sys.executable
        assert "webbrowser.open" in argv[2]
        assert "--resource" not in argv
        parsed = urlsplit(argv[-1])
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{WS}/mcp-service-login"
        assert parse_qs(parsed.query) == {"name": ["system.ai.github"], "o": ["12345"]}
        assert [r.full_url for r in requests] == [SERVICE_URL] + [
            f"{SERVICE_URL}/user-credentials"
        ] * 3
        assert {r.get_header("Authorization") for r in requests} == {"Bearer selected-user-token"}
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] is sys.stderr
        assert kwargs["stderr"] is sys.stderr
        output = capsys.readouterr()
        assert output.out == ""
        assert argv[-1] in output.err
        assert "same Databricks account" in output.err
        assert "selected" in output.err
        assert db_mod._memoized_token(db_mod._token_memo_key(WS, "selected")) is not None

    def test_percent_encoded_name_and_workspace_origin(self, sign_in):
        _, requests, launches, _ = sign_in
        db_mod._remember_token(
            db_mod._token_memo_key(f"{WS}/?o=wrong", "selected"), "selected-user-token", 3600
        )
        ok, _ = mcl.run_connection_login(
            f"{WS}/ai-gateway/mcp-services/my%20catalog.schema.slack%2Bprod/tools/list?ignored=1",
            f"{WS}/?o=wrong",
            profile="selected",
        )
        assert ok
        parsed = urlsplit(launches[0][0][-1])
        assert parse_qs(parsed.query) == {"name": ["my catalog.schema.slack+prod"], "o": ["12345"]}
        assert requests[0].full_url.endswith("/my%20catalog.schema.slack%2Bprod")

    def test_cached_workspace_id_still_validates_service(self, sign_in):
        _, requests, _, _ = sign_in
        db_mod._WORKSPACE_ORG_IDS[urlsplit(WS).hostname] = "777"
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]
        assert [r.full_url for r in requests] == [SERVICE_URL] + [
            f"{SERVICE_URL}/user-credentials"
        ] * 2

    @pytest.mark.parametrize(
        "outcome", [1, OSError("no browser"), subprocess.TimeoutExpired("browser", 10)]
    )
    def test_manual_url_survives_browser_failure(self, sign_in, monkeypatch, capsys, outcome):
        def launch(*args, **kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess(args[0], outcome)

        monkeypatch.setattr(mcl.subprocess_cross_os, "run", launch)
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]
        output = capsys.readouterr()
        assert output.out == ""
        assert "manually" in output.err
        assert f"{WS}/mcp-service-login?name=system.ai.github&o=12345" in output.err

    @pytest.mark.parametrize("state", [404, FAILED])
    def test_browser_login_for_another_user_does_not_succeed(self, sign_in, state):
        states, requests, _, _ = sign_in
        states[:] = [state]
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok
        assert "timed out" in message
        assert "same Databricks account" in message
        assert "selected" in message
        assert "/mcp-service-login?" in message
        assert len(requests) == 5

    @pytest.mark.parametrize(
        "failure",
        [
            401,
            403,
            400,
            b"invalid json",
            [],
            {},
            {"provisioning_info": None},
            {"provisioning_info": {"state": "UNKNOWN"}},
        ],
    )
    def test_inconclusive_or_denied_status_never_reports_success(self, sign_in, failure):
        states, requests, _, _ = sign_in
        states[:] = [404, failure]
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok
        assert "could not check connection sign-in" in message
        assert len(requests) == 3

    @pytest.mark.parametrize(
        "failure", [429, 500, 502, 503, 504, urllib_error.URLError("offline"), TimeoutError("slow")]
    )
    def test_transient_status_errors_can_recover(self, sign_in, failure):
        states, _, _, _ = sign_in
        states[:] = [404, failure, ACTIVE]
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]

    @pytest.mark.parametrize("failure", [429, 503, urllib_error.URLError("offline")])
    def test_transient_pre_browser_credential_error_retries(self, sign_in, failure):
        states, _, launches, _ = sign_in
        states[:] = [failure, 404, ACTIVE]
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]
        assert len(launches) == 1

    def test_transient_status_errors_are_bounded(self, sign_in):
        states, requests, _, _ = sign_in
        states[:] = [404, 503]
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "timed out" in message
        assert len(requests) == 5

    @pytest.mark.parametrize("state", ["PROVISIONING", "UPDATING", "DELETING"])
    def test_pending_credential_state_waits_for_active(self, sign_in, state):
        states, _, _, _ = sign_in
        states[:] = [404, {"provisioning_info": {"state": state}}, ACTIVE]
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]

    @pytest.mark.parametrize("changed", [False, True])
    def test_existing_active_credential_requires_observed_update(self, sign_in, changed):
        states, _, _, _ = sign_in
        updated = {**ACTIVE, "options": {"access_token_expiration": "later"}}
        states[:] = [ACTIVE, ACTIVE, updated] if changed else [ACTIVE]
        ok, detail = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert ok == changed
        assert ("timed out" in detail) == (not changed)

    def test_missing_service_with_cached_workspace_id_fails_before_browser(self, sign_in):
        _, _, launches, metadata = sign_in
        db_mod._WORKSPACE_ORG_IDS[urlsplit(WS).hostname] = "777"
        metadata[:] = [404]
        ok, detail = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "HTTP 404" in detail
        assert not launches

    def test_missing_workspace_id_does_not_open_wrong_workspace(self, sign_in, monkeypatch):
        _, _, launches, _ = sign_in
        monkeypatch.setattr(
            db_mod.urllib_request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"{}")
        )
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "workspace ID" in message
        assert not launches

    def test_metadata_denied_does_not_open_browser(self, sign_in):
        _, _, launches, metadata = sign_in
        metadata[:] = [403]
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "HTTP 403" in message
        assert not launches

    def test_token_refresh_keeps_selected_profile(self, sign_in, monkeypatch):
        states, requests, _, _ = sign_in
        states[:] = [404, ACTIVE]
        calls = []
        tokens = iter(["initial", "refreshed"])

        def get_token(workspace, profile):
            calls.append((workspace, profile))
            return next(tokens)

        monkeypatch.setattr(mcl, "get_databricks_token", get_token)
        assert mcl.run_connection_login(AIGW_URL, WS, profile="selected")[0]
        assert calls == [(WS, "selected")] * 2
        assert [r.get_header("Authorization") for r in requests] == [
            "Bearer initial",
            "Bearer initial",
            "Bearer refreshed",
        ]

    @pytest.mark.parametrize("fail_during_poll", [False, True])
    def test_workspace_auth_failure_is_reported(self, sign_in, monkeypatch, fail_during_poll):
        _, _, launches, _ = sign_in
        calls = []

        def get_token(*args):
            calls.append(args)
            if not fail_during_poll or len(calls) > 1:
                raise RuntimeError("selected profile sign-in expired")
            return "token"

        monkeypatch.setattr(mcl, "get_databricks_token", get_token)
        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "sign-in expired" in message
        assert bool(launches) == fail_during_poll

    @pytest.mark.parametrize(
        "url",
        [
            WS,
            f"{WS}/ai-gateway/mcp-services/",
            f"{WS}/ai-gateway/mcp-services/invalid",
            f"{WS}/ai-gateway/mcp-services/catalog..service",
        ],
    )
    def test_invalid_service_does_not_launch(self, sign_in, url):
        _, requests, launches, _ = sign_in
        ok, message = mcl.run_connection_login(url, WS, profile="selected")
        assert not ok and "catalog.schema.service" in message
        assert not requests and not launches

    def test_poll_finishing_after_deadline_never_reports_success(self, sign_in, monkeypatch):
        _, _, launches, _ = sign_in
        original = db_mod.urllib_request.urlopen
        timeouts = []

        def slow_poll(request, **kwargs):
            if launches and request.full_url.endswith("/user-credentials"):
                timeouts.append(kwargs["timeout"])
                mcl.time.sleep(7)
            return original(request, **kwargs)

        monkeypatch.setattr(db_mod.urllib_request, "urlopen", slow_poll)
        ok, detail = mcl.run_connection_login(AIGW_URL, WS, profile="selected")
        assert not ok and "timed out" in detail
        assert timeouts == [6]
