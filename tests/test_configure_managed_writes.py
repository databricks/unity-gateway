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


def test_root_runs_privileged_commands_without_sudo(monkeypatch):
    # Containers often run as root with no sudo installed.
    monkeypatch.setattr(managed_files.os, "geteuid", lambda: 0, raising=False)

    assert managed_files._sudo_command("rm", "-f", "/etc/x") == ["rm", "-f", "/etc/x"]


@pytest.mark.parametrize("args", [["configure"], ["revert"]], ids=["configure", "revert"])
def test_configure_and_revert_request_explicit_writes(monkeypatch, args):
    def stop(*_a, **_kw):
        raise RuntimeError("stop after the flag is set")

    monkeypatch.setattr(cli_mod, "install_databricks_cli", stop)
    monkeypatch.setattr(cli_mod, "revert", stop)

    CliRunner().invoke(cli_mod.app, args)

    assert managed_files.explicit_managed_writes()


def _write_policy_env(tmp_path, monkeypatch):
    """A real reconcile/revert of one managed file, with privileged writes landing in tmp_path."""
    path = tmp_path / "managed-settings.json"
    monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.LINUX)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: False)

    def write(target, text):
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(managed_files, "_sudo_replace", write)
    monkeypatch.setattr(managed_files, "_sudo_remove", lambda target: target.unlink())
    return path


def _reconcile(path):
    managed_files.reconcile_managed_file(
        path, '{"a": 1}', tool="claude", display="Claude Code", owned_paths=[], parser=json.loads
    )


def _revert():
    return managed_files.revert_managed_file(
        "claude", display="Claude Code", parser=json.loads, dumper=json.dumps
    )


def test_headless_revert_restores_the_managed_file(tmp_path, monkeypatch):
    path = _write_policy_env(tmp_path, monkeypatch)
    managed_files.request_explicit_managed_writes()
    _reconcile(path)
    assert path.exists()

    managed_files.request_explicit_managed_writes("ug revert")
    assert _revert() == "removed"
    assert not path.exists()


def test_denied_headless_revert_keeps_the_backup_and_says_so(tmp_path, monkeypatch):
    path = _write_policy_env(tmp_path, monkeypatch)
    managed_files.request_explicit_managed_writes()
    _reconcile(path)

    def denied(_target):
        raise CalledProcessError(1, ["sudo", "-n", "rm"], stderr="sudo: a password is required")

    monkeypatch.setattr(managed_files, "_sudo_remove", denied)
    managed_files.request_explicit_managed_writes("ug revert")

    with pytest.raises(RuntimeError, match="The backup was retained"):
        _revert()
    assert path.exists()


def test_missing_sudo_is_reported_like_a_denied_write(tmp_path, monkeypatch):
    # A slim container may have no sudo binary at all.
    path = _write_policy_env(tmp_path, monkeypatch)

    def no_sudo(_target, _text):
        raise FileNotFoundError(2, "No such file or directory", "/usr/bin/sudo")

    monkeypatch.setattr(managed_files, "_sudo_replace", no_sudo)
    managed_files.request_explicit_managed_writes()

    with pytest.raises(managed_files.ManagedFileWriteUnavailable, match="as root"):
        _reconcile(path)


@pytest.mark.parametrize(("command", "user_hint"), [("ug configure", True), ("ug revert", False)])
def test_write_failure_names_the_command_to_rerun(tmp_path, monkeypatch, command, user_hint):
    path = _write_policy_env(tmp_path, monkeypatch)

    def denied(_target, _text):
        raise CalledProcessError(1, ["sudo", "-n"], stderr="sudo: a password is required")

    monkeypatch.setattr(managed_files, "_sudo_replace", denied)
    managed_files.request_explicit_managed_writes(command)

    with pytest.raises(managed_files.ManagedFileWriteUnavailable) as raised:
        _reconcile(path)

    assert f"Run `{command}`" in str(raised.value)
    assert ("`--user`" in str(raised.value)) is user_hint


def test_windows_skipped_write_warning_asks_for_elevation(tmp_path, monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(managed_files, "print_warning", warnings.append)
    monkeypatch.setattr(managed_files, "current_os", lambda: managed_files.OS.WINDOWS)

    with managed_files.managed_write_batch(["Codex"]):
        managed_files.warn_managed_write_skipped(tmp_path, {}, {"model_provider": "Databricks"})

    assert len(warnings) == 1
    assert "administrator rights" in warnings[0] and "sudo" not in warnings[0]


def test_configure_fails_on_a_denied_managed_mcp_write(monkeypatch):
    def denied(_managed, _agents):
        raise managed_files.ManagedFileWriteUnavailable("sudo: a password is required")

    monkeypatch.setattr(cli_mod, "reconcile_managed_mcp_servers", denied)
    managed_files.request_explicit_managed_writes()

    with pytest.raises(managed_files.ManagedFileWriteUnavailable):
        cli_mod._configure_managed_mcp_servers({"default_agent": "CODING_AGENT_CODEX"})
