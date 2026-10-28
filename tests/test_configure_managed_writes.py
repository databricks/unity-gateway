"""`ug configure` and `ug revert` write the OS-managed files with or without a terminal.

The write itself settles privilege (root writes directly, a terminal lets sudo prompt, otherwise
`sudo -n`), so an interactive and a headless configure take the same path. Launches keep needing a
terminal or a file to repair. Codex's side is in `test_agent_codex.py`.
"""

import json
from subprocess import CalledProcessError

import pytest
from typer.testing import CliRunner

from tests.test_claude_header_mod import WS, _managed_env
from ucode import cli as cli_mod
from ucode import managed_files
from ucode.agents import claude

MODEL = "databricks-claude-sonnet-4"


def _headless(monkeypatch, *, explicit: bool) -> None:
    """No terminal, the real write policy, and (for configure) the explicit-write flag."""
    monkeypatch.setattr(claude, "managed_writes_allowed", managed_files.managed_writes_allowed)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        managed_files,
        "_print_managed_write_permission",
        lambda *a: pytest.fail("a headless run has nobody to ask for a password"),
    )
    if explicit:
        managed_files.request_explicit_managed_writes()


@pytest.mark.parametrize("seeded", [False, True], ids=["new-file", "existing-file"])
def test_headless_configure_writes_claude_managed_settings_and_the_mod(
    tmp_path, monkeypatch, seeded
):
    managed_path, root, _writes = _managed_env(
        tmp_path, monkeypatch, {"env": {"ADMIN_ONLY": "keep"}} if seeded else None
    )
    _headless(monkeypatch, explicit=True)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, MODEL)

    settings = json.loads(managed_path.read_text())
    assert settings["apiKeyHelper"]
    assert settings["env"]["ANTHROPIC_BASE_URL"].startswith(WS)
    assert settings["env"]["ANTHROPIC_CUSTOM_HEADERS"]
    assert claude.header_mod_active()
    if seeded:
        assert settings["env"]["ADMIN_ONLY"] == "keep"


def test_headless_launch_still_leaves_claude_managed_settings_alone(tmp_path, monkeypatch):
    managed_path, root, writes = _managed_env(tmp_path, monkeypatch)
    monkeypatch.setattr(claude, "_explicit_configure", False)
    _headless(monkeypatch, explicit=False)

    claude.write_tool_config({"workspace": WS, "codex_models": []}, MODEL)

    assert writes == []
    assert not managed_path.exists()
    assert not root.exists()


def test_headless_configure_fails_when_claude_managed_settings_cannot_be_written(
    tmp_path, monkeypatch
):
    managed_path, _root, _writes = _managed_env(tmp_path, monkeypatch)
    _headless(monkeypatch, explicit=True)
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: None)

    def denied(target, _text):
        raise CalledProcessError(1, ["sudo", "-n"], stderr="sudo: a password is required")

    monkeypatch.setattr(managed_files, "_sudo_replace", denied)

    with pytest.raises(managed_files.ManagedFileWriteUnavailable) as raised:
        claude.write_tool_config({"workspace": WS, "codex_models": []}, MODEL)

    message = str(raised.value)
    assert str(managed_path) in message
    assert "as root" in message and "`--user`" in message


def test_unelevated_windows_configure_asks_for_an_elevated_rerun(tmp_path, monkeypatch):
    path = tmp_path / "managed-settings.json"
    monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.WINDOWS)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(
        managed_files, "MANAGED_BACKUP_MANIFEST_PATH", tmp_path / "backups" / "manifest.json"
    )

    def unelevated(_path, _text):
        raise PermissionError("administrator rights are required")

    monkeypatch.setattr(managed_files, "_windows_replace", unelevated)
    managed_files.request_explicit_managed_writes()

    with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="requires administrator"):
        managed_files.reconcile_managed_file(
            path, "{}", tool="claude", display="Claude Code", owned_paths=[], parser=json.loads
        )


@pytest.mark.parametrize("os_name", ["LINUX", "MACOS", "WINDOWS"])
@pytest.mark.parametrize("interactive", [False, True])
def test_configure_may_always_write(monkeypatch, os_name, interactive):
    monkeypatch.setattr(managed_files, "current_os", lambda: getattr(managed_files.OS, os_name))
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: interactive)
    managed_files.request_explicit_managed_writes()

    assert managed_files.managed_writes_allowed()

    managed_files.request_explicit_managed_writes(False)
    assert managed_files.managed_writes_allowed() is (interactive and os_name != "WINDOWS")


def test_root_runs_privileged_commands_without_sudo(monkeypatch):
    # Containers often run as root with no sudo installed.
    monkeypatch.setattr(managed_files.system_cross_os, "is_admin", lambda: True)

    assert managed_files._sudo_command("rm", "-f", "/etc/x") == ["rm", "-f", "/etc/x"]


def test_non_root_privileged_commands_never_prompt_without_a_terminal(monkeypatch):
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)

    assert managed_files._sudo_command("rm", "-f", "/etc/x")[1:3] == ["-n", "rm"]


@pytest.mark.parametrize("args", [["configure"], ["revert"]], ids=["configure", "revert"])
def test_configure_and_revert_request_explicit_writes(monkeypatch, args):
    def stop(*_a, **_kw):
        raise RuntimeError("stop after the flag is set")

    monkeypatch.setattr(cli_mod, "install_databricks_cli", stop)
    monkeypatch.setattr(cli_mod, "revert", stop)

    CliRunner().invoke(cli_mod.app, args)

    assert managed_files.explicit_managed_writes()
