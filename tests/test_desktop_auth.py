"""Focused, network-free tests for the Claude Desktop credential helper."""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

import ucode.desktop_auth as desktop_auth

HOST = "https://workspace.example.com"
PROFILE = "cowork-profile"
CLI = "/opt/databricks/bin/databricks"


def _result(returncode: int = 0, *, token: str | None = None, stderr: str = ""):
    stdout = json.dumps({"access_token": token}) if token is not None else ""
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _clean_auth_environment(monkeypatch):
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    monkeypatch.delenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, raising=False)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(desktop_auth, "databricks_cli_path", lambda: CLI)


def test_valid_cached_token_is_clean_and_uses_selected_profile(cli, monkeypatch, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result(token="cached-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_token(HOST, PROFILE) == "cached-token"

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        CLI,
        "auth",
        "token",
        "--host",
        HOST,
        "--profile",
        PROFILE,
        "--output",
        "json",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["capture_output"] is True


def test_omitted_profile_is_not_selected_from_host(cli, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return _result(token="default-profile-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_token(HOST) == "default-profile-token"
    assert "--profile" not in seen["argv"]


def test_noninteractive_auth_failure_does_not_start_login(cli, monkeypatch):
    run = Mock(return_value=_result(1, stderr="expired OAuth session"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="new Cowork task"):
        desktop_auth.get_claude_desktop_token(HOST, PROFILE)
    run.assert_called_once()
    assert "login" not in run.call_args.args[0]


def test_interactive_recovery_opens_one_browser_and_prints_no_token(
    cli, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[2:4] == ["token", "--host"]:
            token_calls = sum(call[0][2:4] == ["token", "--host"] for call in calls)
            return (
                _result(token="browser-token")
                if token_calls == 3
                else _result(1, stderr="expired OAuth session")
            )
        return _result()

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_token(HOST, PROFILE) == "browser-token"
    assert len(calls) == 4
    browser_calls = [
        call
        for call in calls
        if call[0][2:4] == ["login", "--host"] and "--no-browser" not in call[0]
    ]
    assert len(browser_calls) == 1
    browser_argv, browser_kwargs = browser_calls[0]
    assert browser_argv == [CLI, "auth", "login", "--host", HOST, "--profile", PROFILE]
    assert browser_kwargs["stdin"] is subprocess.DEVNULL
    assert browser_kwargs["stdout"] is sys.stderr
    assert browser_kwargs["stderr"] is sys.stderr
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_interactive_browser_login_routes_both_streams_to_stderr(cli, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return _result()

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)
    attempt = desktop_auth._run_login(
        CLI,
        HOST,
        PROFILE,
        timeout=desktop_auth.CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS,
    )

    assert not attempt.failure
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is sys.stderr
    assert seen["stderr"] is sys.stderr
    assert seen["timeout"] == desktop_auth.CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS


def test_network_failure_does_not_trigger_login_or_browser(cli, monkeypatch):
    run = Mock(return_value=_result(1, stderr="Could not resolve host"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="network"):
        desktop_auth.get_claude_desktop_token(HOST, PROFILE)
    run.assert_called_once()


def test_absent_cli_fails_before_starting_a_process(monkeypatch):
    monkeypatch.setattr(desktop_auth, "databricks_cli_path", lambda: "databricks")
    monkeypatch.setattr(desktop_auth.shutil, "which", lambda _: None)
    run = Mock(side_effect=AssertionError("CLI must not run when it is absent"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="Databricks CLI"):
        desktop_auth.get_claude_desktop_token(HOST, PROFILE)
    run.assert_not_called()


def test_cancelled_interactive_login_is_actionable(cli, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[2:4] == ["token", "--host"]:
            return _result(1, stderr="expired OAuth session")
        return _result(1, stderr="login cancelled")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="sign-in did not complete"):
        desktop_auth.get_claude_desktop_token(HOST, PROFILE)
    assert sum("login" in command for command in calls) == 1
    assert capsys.readouterr().err == ""


def test_malformed_token_is_rejected_without_leaking_value(cli, monkeypatch, capsys):
    run = Mock(return_value=_result(token="token\nwith-newline"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="new Cowork task"):
        desktop_auth.get_claude_desktop_token(HOST, PROFILE)
    output = capsys.readouterr()
    assert "token\nwith-newline" not in output.out + output.err


def test_force_refresh_is_forwarded_to_token_command(cli, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return _result(token="fresh-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)
    assert desktop_auth.get_claude_desktop_token(HOST, PROFILE, force_refresh=True) == "fresh-token"
    assert seen["argv"][-1] == "--force-refresh"
