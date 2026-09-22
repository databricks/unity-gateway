"""Tests for the per-connection MCP login helpers (mcp_connection_login).

Network-free: URL/connection parsing and the login runner with the subprocess
monkeypatched.
"""

from __future__ import annotations

import subprocess

from ucode import mcp_connection_login as mcl

WS = "https://ws.staging.cloud.databricks.com"
AIGW_URL = f"{WS}/ai-gateway/mcp-services/system.ai.github"


class TestConnectionFromUrl:
    def test_plain_endpoint(self):
        assert mcl.connection_from_url(AIGW_URL) == "system.ai.github"

    def test_with_trailing_path_and_query(self):
        assert mcl.connection_from_url(f"{AIGW_URL}/tools/list?x=1") == "system.ai.github"

    def test_non_aigw_url_is_none(self):
        assert mcl.connection_from_url(f"{WS}/api/2.0/mcp/functions/system/ai") is None

    def test_missing_service_is_none(self):
        assert mcl.connection_from_url(f"{WS}/ai-gateway/mcp-services/") is None


class TestConnectionCredentialState:
    """The gate that stops the proxy re-running the browser login every session."""

    _DETAILS = {
        "id": "svc-1",
        "config": {
            "source_connection": {
                "name": "connections/github",
                "securable_kind": "CONNECTION_HTTP_OAUTH_U2M_MAPPING",
            }
        },
    }

    def _wire(self, monkeypatch, *, details=_DETAILS, details_err=None, cred=None, cred_err=None):
        monkeypatch.setattr(mcl, "get_databricks_token", lambda ws, profile: "tok")
        monkeypatch.setattr(mcl, "_scim_me", lambda ws, token: {"userName": "u@databricks.com"})

        def _http(url, token, *a, **k):
            if "/mcp-services/" in url:
                return details, details_err
            if "/user-credentials/" in url:
                return cred, cred_err
            return None, "unexpected url"

        monkeypatch.setattr(mcl, "_http_get_json", _http)

    def test_present_when_credential_active(self, monkeypatch):
        self._wire(
            monkeypatch,
            cred={"connection_user_credential": {"provisioning_info": {"state": "ACTIVE"}}},
        )
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_PRESENT

    def test_missing_on_404(self, monkeypatch):
        self._wire(monkeypatch, cred_err="HTTP 404 Not Found")
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_MISSING

    def test_missing_when_state_not_active(self, monkeypatch):
        self._wire(
            monkeypatch,
            cred={"connection_user_credential": {"provisioning_info": {"state": "PROVISIONING"}}},
        )
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_MISSING

    def test_no_login_for_non_oauth_connection(self, monkeypatch):
        details = {
            "id": "x",
            "config": {
                "source_connection": {"name": "connections/c", "securable_kind": "CONNECTION_MYSQL"}
            },
        }
        self._wire(monkeypatch, details=details)
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_NO_LOGIN

    def test_unknown_on_service_lookup_error(self, monkeypatch):
        self._wire(monkeypatch, details=None, details_err="HTTP 500")
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_UNKNOWN

    def test_unknown_on_non_404_credential_error(self, monkeypatch):
        self._wire(monkeypatch, cred_err="HTTP 403 Forbidden")
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_UNKNOWN

    def test_unknown_when_token_unavailable(self, monkeypatch):
        def boom(ws, profile):
            raise RuntimeError("no token")

        monkeypatch.setattr(mcl, "get_databricks_token", boom)
        assert mcl.connection_credential_state(AIGW_URL, WS) == mcl.CREDENTIAL_UNKNOWN

    def test_no_login_for_non_aigw_url(self, monkeypatch):
        assert (
            mcl.connection_credential_state(f"{WS}/api/2.0/mcp/functions/x", WS)
            == mcl.CREDENTIAL_NO_LOGIN
        )


class TestRunConnectionLogin:
    def _fake_run(self, captured, *, returncode, stderr=""):
        def _run(argv, **kwargs):
            # The `--resource`-support pre-check runs `auth login --help` first.
            if "--help" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="--resource stringArray", stderr=""
                )
            captured.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout="", stderr=stderr)

        return _run

    def test_success_sends_resource_and_host_without_client_id(self, monkeypatch):
        captured: list[list[str]] = []
        monkeypatch.setattr(mcl.subprocess, "run", self._fake_run(captured, returncode=0))

        ok, message = mcl.run_connection_login(AIGW_URL, WS, profile="p")

        assert ok and message == "signed in"
        argv = captured[0]
        assert argv[:3] == ["databricks", "auth", "login"]
        assert "--resource" in argv and AIGW_URL in argv
        assert "--host" in argv and WS in argv
        assert "--profile" in argv and "p" in argv
        # Uses the CLI's default client (its own registered redirect), so no --client-id.
        assert "--client-id" not in argv

    def test_nonzero_exit_reports_failure(self, monkeypatch):
        # The CLI's own output streams live to stderr (the agent's MCP log), so on
        # failure we return a pointer to that log rather than captured text.
        captured: list[list[str]] = []
        monkeypatch.setattr(mcl.subprocess, "run", self._fake_run(captured, returncode=1))
        ok, message = mcl.run_connection_login(AIGW_URL, WS)
        assert not ok
        assert "did not complete" in message and "1" in message

    def test_output_is_routed_to_stderr_not_stdout(self, monkeypatch):
        # stdout must never be captured to the proxy's stdout (the MCP wire); the
        # CLI's URL/prompts go to this process's stderr.
        seen: dict = {}

        def _run(argv, **kw):
            if "--help" in argv:  # the --resource pre-check; not the login call under test
                return subprocess.CompletedProcess(argv, 0, stdout="--resource", stderr="")
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(mcl.subprocess, "run", _run)
        ok, _ = mcl.run_connection_login(AIGW_URL, WS)
        assert ok
        assert seen.get("stdout") is mcl.sys.stderr
        assert seen.get("stderr") is mcl.sys.stderr
        assert "capture_output" not in seen

    def test_timeout_is_reported(self, monkeypatch):
        def _run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        monkeypatch.setattr(mcl.subprocess, "run", _run)
        ok, message = mcl.run_connection_login(AIGW_URL, WS)
        assert not ok and "timed out" in message

    def test_binary_missing_is_reported(self, monkeypatch):
        def _run(argv, **kwargs):
            raise OSError("not found")

        monkeypatch.setattr(mcl.subprocess, "run", _run)
        ok, message = mcl.run_connection_login(AIGW_URL, WS)
        assert not ok and "could not run" in message

    def test_old_cli_without_resource_flag_reports_clearly(self, monkeypatch):
        # `auth login --help` lacking `--resource` => an old CLI (no databricks/cli#6621).
        # We must report that clearly and never attempt the login (the flag would error).
        def _run(argv, **kwargs):
            if "--help" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, stdout="usage: login [--host]", stderr=""
                )
            raise AssertionError("login must not run when --resource is unsupported")

        monkeypatch.setattr(mcl.subprocess, "run", _run)
        ok, message = mcl.run_connection_login(AIGW_URL, WS)
        assert not ok
        assert "--resource" in message and "Upgrade" in message
