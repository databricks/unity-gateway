"""Focused, network-free tests for the Claude Desktop credential helper."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

import ucode.databricks as databricks
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
    monkeypatch.delenv(desktop_auth.CLAUDE_CODE_OAUTH_TOKEN_ENV, raising=False)
    monkeypatch.delenv(desktop_auth.CLAUDE_CONFIG_DIR_ENV, raising=False)


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(desktop_auth, "databricks_cli_path", lambda: CLI)


def test_valid_cached_token_is_clean_and_uses_selected_profile(cli, monkeypatch, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _result(token="cached-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "cached-token"

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

    assert desktop_auth.get_claude_desktop_databricks_token(HOST) == "default-profile-token"
    assert "--profile" not in seen["argv"]


@pytest.mark.parametrize("needs_login", [False, True])
def test_workspace_helper_clears_legacy_connector_resource_only_before_login(
    cli, monkeypatch, tmp_path, needs_login
):
    config = tmp_path / "databrickscfg"
    cleaned = f"[{PROFILE}]\nhost = {HOST}\nauth_type = databricks-cli\n"
    original = cleaned + f"resources = {HOST}/ai-gateway/mcp-services/system.ai.atlassian\n"
    config.write_text(original)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    calls = []
    signed_in = False

    def run(argv, **kwargs):
        nonlocal signed_in
        calls.append(argv)
        assert config.read_text() == (cleaned if signed_in or argv[2] == "login" else original)
        if argv[1:3] == ["auth", "login"]:
            signed_in = True
            return _result()
        if needs_login and not signed_in:
            return _result(1, stderr="no cached credentials")
        return _result(token="workspace-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "workspace-token"
    assert config.read_text() == (cleaned if needs_login else original)
    assert [argv[1:3] for argv in calls] == (
        [["auth", "token"], ["auth", "token"], ["auth", "login"], ["auth", "token"]]
        if needs_login
        else [["auth", "token"]]
    )
    assert all(argv[argv.index("--profile") + 1] == PROFILE for argv in calls)


def test_resource_cleanup_failure_is_config_error_without_login(cli, monkeypatch, tmp_path):
    config = tmp_path / "databrickscfg"
    config.write_text(
        f"[{PROFILE}]\nhost = {HOST}\nauth_type = databricks-cli\n"
        f"resources = {HOST}/ai-gateway/mcp-services/system.ai.atlassian\n"
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    cli_run = Mock(return_value=_result(1, stderr="no cached credentials"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", cli_run)

    def replace(*args):
        raise PermissionError("read-only config")

    monkeypatch.setattr(databricks.os, "replace", replace)

    with pytest.raises(
        desktop_auth.ClaudeDesktopAuthError, match="Remove its MCP service URLs"
    ) as exc:
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)

    assert exc.value.kind == "config"
    assert [call.args[0][1:3] for call in cli_run.call_args_list] == [["auth", "token"]] * 2


def test_external_bearer_uses_shared_token_path(monkeypatch):
    monkeypatch.setenv("DATABRICKS_BEARER", "external-token")
    token = Mock(return_value="external-token")
    monkeypatch.setattr(desktop_auth, "get_databricks_token", token)
    monkeypatch.setattr(
        desktop_auth,
        "databricks_cli_path",
        Mock(side_effect=AssertionError("CLI must not run for an external bearer")),
    )

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "external-token"
    token.assert_called_once_with(HOST, PROFILE, force_refresh=False)


def test_noninteractive_auth_failure_does_not_start_login(cli, monkeypatch):
    run = Mock(return_value=_result(1, stderr="expired OAuth session"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="new Cowork task"):
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
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

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "browser-token"
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
    desktop_auth._run_login(
        CLI,
        HOST,
        PROFILE,
        timeout=desktop_auth.CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS,
    )

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is sys.stderr
    assert seen["stderr"] is sys.stderr
    assert seen["timeout"] == desktop_auth.CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS


def test_network_failure_does_not_trigger_login_or_browser(cli, monkeypatch):
    run = Mock(return_value=_result(1, stderr="Could not resolve host"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="network"):
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
    run.assert_called_once()


@pytest.mark.parametrize("interactive", [False, True])
def test_absent_cli_fails_before_starting_a_process(monkeypatch, interactive):
    monkeypatch.setattr(databricks, "_iter_databricks_executables", lambda: [])
    if interactive:
        monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    run = Mock(side_effect=AssertionError("CLI must not run when it is absent"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="Databricks CLI"):
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
    run.assert_not_called()


@pytest.mark.parametrize("expired", [False, True])
def test_shared_cli_discovery_pins_newest_binary_through_recovery(monkeypatch, tmp_path, expired):
    older_cli = str(tmp_path / "old cli" / "databricks")
    newer_cli = str(tmp_path / "new cli" / "databricks")
    versions = {older_cli: (1, 0, 0), newer_cli: (1, 20, 0)}
    monkeypatch.setattr(databricks, "_iter_databricks_executables", lambda: list(versions))
    monkeypatch.setattr(databricks, "_read_databricks_cli_version", versions.__getitem__)
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    responses = [_result(token="fresh-token")]
    if expired:
        responses = [
            _result(1, stderr="expired"),
            _result(1, stderr="expired"),
            _result(),
            *responses,
        ]
    run = Mock(side_effect=responses)
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "fresh-token"

    assert [call.args[0][0] for call in run.call_args_list] == [newer_cli] * len(responses)
    assert [call.args[0][2] for call in run.call_args_list] == (
        ["token", "token", "login", "token"] if expired else ["token"]
    )


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
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
    assert sum("login" in command for command in calls) == 1
    assert capsys.readouterr().err == ""


def test_malformed_token_is_rejected_without_leaking_value(cli, monkeypatch, capsys):
    run = Mock(return_value=_result(token="token\nwith-newline"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="new Cowork task"):
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
    output = capsys.readouterr()
    assert "token\nwith-newline" not in output.out + output.err


def test_force_refresh_is_forwarded_to_token_command(cli, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        return _result(token="fresh-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)
    assert (
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE, force_refresh=True)
        == "fresh-token"
    )
    assert seen["argv"][-1] == "--force-refresh"


def test_token_rechecked_under_lock_avoids_second_browser(cli, monkeypatch, tmp_path):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    lock_path = tmp_path / "auth.lock"
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", lock_path)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv[2])
        if len(commands) == 1:
            return _result(1, stderr="expired")
        with lock_path.open("a+b") as other_lock:
            with pytest.raises(TimeoutError):
                desktop_auth.acquire_exclusive_file_lock(other_lock, timeout=0)
        return _result(token="other-helper-token")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE) == "other-helper-token"
    assert commands == ["token", "token"]
    with lock_path.open("a+b") as lock_file:
        desktop_auth.acquire_exclusive_file_lock(lock_file, timeout=0)
        desktop_auth.release_file_lock(lock_file)


@pytest.mark.parametrize("stage", ["initial", "recheck", "login", "final"])
@pytest.mark.parametrize(
    ("failure", "kind", "message"),
    [
        (_result(1, stderr="Could not resolve host"), "network", "Check your network"),
        (subprocess.TimeoutExpired("databricks", 15), "network", "Check your network"),
        (PermissionError("access denied"), "cli", "Install or repair"),
    ],
    ids=["network", "timeout", "cli"],
)
def test_recovery_failure_stops_and_releases_lock(
    cli, monkeypatch, tmp_path, stage, failure, kind, message
):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    lock_path = tmp_path / "auth.lock"
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", lock_path)
    # Substitute only the external CLI; exercise real recovery and file locking.
    stages = ["initial", "recheck", "login", "final"]
    responses = [_result(1, stderr="expired"), _result(1, stderr="expired"), _result()]
    index = stages.index(stage)
    run = Mock(side_effect=[*responses[:index], failure])
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    # Login output is streamed, so a nonzero exit has no captured network detail.
    if stage == "login" and isinstance(failure, subprocess.CompletedProcess):
        kind, message = "auth", "sign-in did not complete"
    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match=message) as raised:
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)

    assert raised.value.kind == kind
    assert run.call_count == index + 1
    with lock_path.open("a+b") as lock_file:
        desktop_auth.acquire_exclusive_file_lock(lock_file, timeout=0)
        desktop_auth.release_file_lock(lock_file)


def test_waiting_for_login_lock_stops_at_deadline(cli, monkeypatch, tmp_path):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    lock_path = tmp_path / "auth.lock"
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", lock_path)
    now = 0.0
    monkeypatch.setattr(desktop_auth.time, "monotonic", lambda: now)

    def advance_to_deadline(_seconds):
        nonlocal now
        now = 270.0

    monkeypatch.setattr(desktop_auth.time, "sleep", advance_to_deadline)
    run = Mock(return_value=_result(1, stderr="expired"))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with lock_path.open("a+b") as held_lock:
        desktop_auth.acquire_exclusive_file_lock(held_lock)
        try:
            with pytest.raises(
                desktop_auth.ClaudeDesktopAuthError, match="another sign-in"
            ) as raised:
                desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)
            assert raised.value.kind == "network"
            run.assert_called_once()
            with lock_path.open("a+b") as other_lock:
                with pytest.raises(TimeoutError):
                    desktop_auth.acquire_exclusive_file_lock(other_lock, timeout=0)
        finally:
            desktop_auth.release_file_lock(held_lock)


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json-secret",
        "[]",
        "{}",
        '{"access_token":123}',
        '{"access_token":""}',
        '{"access_token":"secret with spaces"}',
        '{"access_token":"secret\\u0000"}',
    ],
)
def test_invalid_token_output_stays_out_of_errors(cli, monkeypatch, capsys, stdout):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError) as raised:
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)

    assert str(raised.value) == (
        "Databricks authentication expired. Start a new Cowork task to sign in, then retry."
    )
    assert raised.value.kind == "auth"
    assert capsys.readouterr() == ("", "")
    run.assert_called_once()


def test_browser_and_token_fetch_share_remaining_budget(cli, monkeypatch, tmp_path):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    now = 0.0
    monkeypatch.setattr(desktop_auth.time, "monotonic", lambda: now)
    timeouts = []
    elapsed = iter([10.0, 20.0, 260.0, 260.0])
    responses = iter(
        [
            _result(1, stderr="expired"),
            _result(1, stderr="expired"),
            _result(),
            _result(token="new-token"),
        ]
    )

    def run(argv, **kwargs):
        nonlocal now
        if argv[2] == "token":
            assert argv[-1] == "--force-refresh"
        timeouts.append(kwargs["timeout"])
        now = next(elapsed)
        return next(responses)

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert (
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE, force_refresh=True)
        == "new-token"
    )
    assert timeouts == [15.0, 15.0, 250.0, 10.0]


def test_budget_exhausted_after_login_stops_before_final_fetch(cli, monkeypatch, tmp_path):
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    lock_path = tmp_path / "auth.lock"
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", lock_path)
    now = 0.0
    monkeypatch.setattr(desktop_auth.time, "monotonic", lambda: now)
    calls = []

    def run(argv, **kwargs):
        nonlocal now
        calls.append(argv[2])
        if argv[2] == "login":
            now = 270.0
            return _result()
        return _result(1, stderr="expired")

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="refresh timed out") as raised:
        desktop_auth.get_claude_desktop_databricks_token(HOST, PROFILE)

    assert raised.value.kind == "network"
    assert calls == ["token", "token", "login"]
    with lock_path.open("a+b") as lock_file:
        desktop_auth.acquire_exclusive_file_lock(lock_file, timeout=0)
        desktop_auth.release_file_lock(lock_file)


def test_relay_credential_combines_keychain_and_databricks_tokens(monkeypatch):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    credentials = {
        "claudeAiOauth": {
            "accessToken": "anthropic-token",
            "expiresAt": int((time.time() + 3600) * 1000),
        }
    }
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(credentials), stderr="")
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    assert desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/Applications/Claude Code.app/Contents/MacOS/claude"
    ) == {
        "token": "anthropic-token",
        "headers": {
            desktop_auth.AI_GATEWAY_TOKEN_HEADER: "Bearer databricks-token",
        },
    }
    assert run.call_args.args[0] == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "desktop-user",
        "-s",
        "Claude Code-credentials",
        "-w",
    ]


def test_relay_credential_uses_custom_config_keychain_service(monkeypatch):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv(desktop_auth.CLAUDE_CONFIG_DIR_ENV, "/custom/claude")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    credentials = {
        "claudeAiOauth": {
            "accessToken": "anthropic-token",
            "expiresAt": int((time.time() + 3600) * 1000),
        }
    }
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(credentials), stderr="")
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/Applications/Claude Code.app/Contents/MacOS/claude"
    )

    assert run.call_args.args[0][5] == "Claude Code-credentials-7427f042"


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("custom_config_dir", [False, True])
@pytest.mark.parametrize("with_bom", [False, True])
def test_relay_credential_reads_file_credentials_on_linux_and_windows(
    monkeypatch, tmp_path, platform, custom_config_dir, with_bom
):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: platform)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    if custom_config_dir:
        config_dir = tmp_path / "custom claude config"
        monkeypatch.setenv(desktop_auth.CLAUDE_CONFIG_DIR_ENV, str(config_dir))
    else:
        config_dir = home / ".claude"
    config_dir.mkdir(parents=True)
    credentials = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "anthropic-token",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        }
    )
    if with_bom:
        credentials = "\ufeff" + credentials
    (config_dir / ".credentials.json").write_text(credentials, encoding="utf-8")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    run = Mock()
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/opt/claude/bin/claude"
    )

    assert credential == {
        "token": "anthropic-token",
        "headers": {
            desktop_auth.AI_GATEWAY_TOKEN_HEADER: "Bearer databricks-token",
        },
    }
    run.assert_not_called()


@pytest.mark.parametrize("expires_at", [None, "1770000000000", True, float("nan"), float("inf")])
def test_subscription_credential_rejects_invalid_expiry(monkeypatch, expires_at):
    monkeypatch.setattr(
        desktop_auth,
        "_read_claude_credentials",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "anthropic-token",
                "expiresAt": expires_at,
            }
        },
    )

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="credentials are invalid"):
        desktop_auth._claude_subscription_token()


def test_relay_credential_uses_explicit_claude_oauth_token(monkeypatch):
    monkeypatch.setenv(desktop_auth.CLAUDE_CODE_OAUTH_TOKEN_ENV, "anthropic-token")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    run = Mock()
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/opt/claude/bin/claude"
    )

    assert credential["token"] == "anthropic-token"
    run.assert_not_called()


@pytest.mark.parametrize("configured_token", ["", " \t "])
def test_relay_credential_ignores_empty_explicit_claude_oauth_token(monkeypatch, configured_token):
    monkeypatch.setenv(desktop_auth.CLAUDE_CODE_OAUTH_TOKEN_ENV, configured_token)
    monkeypatch.setattr(
        desktop_auth,
        "_read_claude_credentials",
        lambda: {
            "claudeAiOauth": {
                "accessToken": "stored-token",
                "expiresAt": int((time.time() + 3600) * 1000),
            }
        },
    )

    assert desktop_auth._claude_subscription_token() == "stored-token"


def test_relay_credential_rejects_invalid_explicit_claude_oauth_token(monkeypatch):
    monkeypatch.setenv(desktop_auth.CLAUDE_CODE_OAUTH_TOKEN_ENV, "invalid token")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    run = Mock()
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(
        desktop_auth.ClaudeDesktopAuthError,
        match="Unset it to use Claude's normal sign-in flow",
    ) as raised:
        desktop_auth.get_claude_desktop_relay_credential(HOST, PROFILE, "/opt/claude/bin/claude")

    assert raised.value.kind == "config"
    run.assert_not_called()


def _subscription_credentials(expires_in_seconds: int, token: str) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "expiresAt": int((time.time() + expires_in_seconds) * 1000),
            }
        }
    )


@pytest.mark.parametrize("helper_context", [None, "background", "mid-session-refresh"])
def test_relay_credential_silently_refreshes_expiring_token(monkeypatch, tmp_path, helper_context):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    if helper_context is not None:
        monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, helper_context)
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                [], 0, stdout=_subscription_credentials(60, "old-token"), stderr=""
            ),
            subprocess.CompletedProcess(
                [], 0, stdout=_subscription_credentials(60, "old-token"), stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=_subscription_credentials(3600, "fresh-token"), stderr=""
            ),
        ]
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/opt/claude/bin/claude"
    )

    assert credential["token"] == "fresh-token"
    assert [call.args[0][1] for call in run.call_args_list] == [
        "find-generic-password",
        "find-generic-password",
        "auth",
        "find-generic-password",
    ]
    assert run.call_args_list[2].args[0][1:3] == ["auth", "status"]


def test_relay_credential_rediscovers_stale_claude_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(desktop_auth.shutil, "which", lambda binary: "/new/bin/claude")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    expired = _subscription_credentials(60, "old-token")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            FileNotFoundError,
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=_subscription_credentials(3600, "fresh-token"), stderr=""
            ),
        ]
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(HOST, PROFILE, "/old/bin/claude")

    assert credential["token"] == "fresh-token"
    assert run.call_args_list[2].args[0][0] == "/old/bin/claude"
    assert run.call_args_list[3].args[0][0] == desktop_auth.os.path.abspath("/new/bin/claude")


def test_relay_credential_noninteractive_refresh_failure_does_not_open_browser(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    expired = _subscription_credentials(60, "old-token")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not refreshed"),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
        ]
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="expired"):
        desktop_auth.get_claude_desktop_relay_credential(HOST, PROFILE, "/opt/claude/bin/claude")

    assert [call.args[0][1:3] for call in run.call_args_list if call.args[0][1] == "auth"] == [
        ["auth", "status"]
    ]


def test_relay_credential_logs_in_when_silent_refresh_does_not_renew(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    expired = _subscription_credentials(60, "old-token")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not refreshed"),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=_subscription_credentials(3600, "fresh-token"), stderr=""
            ),
        ]
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/opt/claude/bin/claude"
    )

    assert credential["token"] == "fresh-token"
    assert [call.args[0][1:3] for call in run.call_args_list if call.args[0][1] == "auth"] == [
        ["auth", "status"],
        ["auth", "login"],
    ]


def test_relay_claude_refresh_and_login_share_helper_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    now = 0.0
    monkeypatch.setattr(desktop_auth.time, "monotonic", lambda: now)
    expired = _subscription_credentials(60, "old-token")
    fresh = _subscription_credentials(3600, "fresh-token")
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not refreshed"),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=fresh, stderr=""),
        ]
    )
    auth_timeouts = []

    def run(argv, **kwargs):
        nonlocal now
        now += 10
        if argv[1] == "auth":
            auth_timeouts.append((argv[2], kwargs["timeout"]))
        return next(responses)

    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    credential = desktop_auth.get_claude_desktop_relay_credential(
        HOST, PROFILE, "/opt/claude/bin/claude"
    )

    assert credential["token"] == "fresh-token"
    assert auth_timeouts == [("status", 15), ("login", 230)]


def test_relay_credential_reports_failed_claude_login(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_auth, "_credential_storage_platform", lambda: "darwin")
    monkeypatch.setenv("USER", "desktop-user")
    monkeypatch.setenv(desktop_auth.CLAUDE_HELPER_CONTEXT_ENV, "interactive")
    monkeypatch.setattr(desktop_auth, "_AUTH_LOCK_PATH", tmp_path / "auth.lock")
    monkeypatch.setattr(
        desktop_auth,
        "get_claude_desktop_databricks_token",
        lambda host, profile: "databricks-token",
    )
    expired = _subscription_credentials(60, "old-token")
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="not refreshed"),
            subprocess.CompletedProcess([], 0, stdout=expired, stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="cancelled"),
        ]
    )
    monkeypatch.setattr(desktop_auth.subprocess_cross_os, "run", run)

    with pytest.raises(desktop_auth.ClaudeDesktopAuthError, match="sign-in failed"):
        desktop_auth.get_claude_desktop_relay_credential(HOST, PROFILE, "/opt/claude/bin/claude")
