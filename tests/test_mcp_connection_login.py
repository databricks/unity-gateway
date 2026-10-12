"""Tests for the per-connection MCP login helpers (mcp_connection_login).

Network-free: URL/connection parsing and the login runner with the subprocess
monkeypatched.
"""

from __future__ import annotations

import subprocess

import pytest

from ucode import mcp_connection_login as mcl

WS = "https://ws.staging.cloud.databricks.com"
AIGW_URL = f"{WS}/ai-gateway/mcp-services/system.ai.github"
RESOLVED_CLI = "/opt/databricks/bin/databricks"


class TestConnectionFromUrl:
    def test_plain_endpoint(self):
        assert mcl.connection_from_url(AIGW_URL) == "system.ai.github"

    def test_with_trailing_path_and_query(self):
        assert mcl.connection_from_url(f"{AIGW_URL}/tools/list?x=1") == "system.ai.github"

    def test_non_aigw_url_is_none(self):
        assert mcl.connection_from_url(f"{WS}/api/2.0/mcp/functions/system/ai") is None

    def test_missing_service_is_none(self):
        assert mcl.connection_from_url(f"{WS}/ai-gateway/mcp-services/") is None


class TestRunConnectionLogin:
    @pytest.fixture(autouse=True)
    def _resolved_cli(self, monkeypatch):
        monkeypatch.setattr(mcl, "databricks_cli_path", lambda: RESOLVED_CLI)

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
        assert argv[0] == RESOLVED_CLI
        assert argv[1:3] == ["auth", "login"]
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
