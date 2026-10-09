"""Desktop credential command output, argument isolation, and failure behavior."""

import pytest
from typer.testing import CliRunner

from ucode.cli import app


@pytest.fixture
def runner():
    return CliRunner()


def test_desktop_auth_emits_only_token_and_keeps_diagnostics_on_stderr(monkeypatch, runner):
    from ucode import desktop_auth

    def credential(host, profile):
        assert host == "https://workspace.example"
        assert profile == "selected-profile"
        print("Sign-in diagnostic")
        return "test-access-token"

    monkeypatch.setattr(desktop_auth, "get_claude_desktop_token", credential)
    result = runner.invoke(
        app,
        [
            "claude-desktop-auth",
            "--host",
            "https://workspace.example",
            "--profile",
            "selected-profile",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout == "test-access-token\n"
    assert "Sign-in diagnostic" in result.stderr


@pytest.mark.parametrize("missing", ["host", "profile"])
def test_desktop_auth_requires_explicit_workspace_and_profile(runner, missing):
    args = ["claude-desktop-auth"]
    if missing != "host":
        args += ["--host", "https://workspace.example"]
    if missing != "profile":
        args += ["--profile", "selected-profile"]
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert result.stdout == ""
    assert f"--{missing}" in result.stderr


@pytest.mark.parametrize("error", [RuntimeError("Login required"), ValueError("Invalid host")])
def test_desktop_auth_failure_is_actionable_and_never_emits_token(monkeypatch, runner, error):
    from ucode import desktop_auth

    def credential(host, profile):
        raise error

    monkeypatch.setattr(desktop_auth, "get_claude_desktop_token", credential)
    result = runner.invoke(
        app,
        ["claude-desktop-auth", "--host", "https://workspace.example", "--profile", "selected"],
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert str(error) in result.stderr


def test_desktop_auth_cancelled_login_is_reported_on_stderr(monkeypatch, runner):
    from ucode import desktop_auth

    def credential(host, profile):
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop_auth, "get_claude_desktop_token", credential)
    result = runner.invoke(
        app,
        ["claude-desktop-auth", "--host", "https://workspace.example", "--profile", "selected"],
    )
    assert result.exit_code == 130
    assert result.stdout == ""
    assert "cancelled" in result.stderr


def test_desktop_auth_is_hidden_from_public_help(runner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "claude-desktop-auth" not in result.stdout
