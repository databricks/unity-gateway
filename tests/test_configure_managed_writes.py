"""`ug configure` and `ug revert` write the OS-managed files with or without a terminal.

The write itself settles privilege (root writes directly, a terminal lets sudo prompt, otherwise
`sudo -n`), so an interactive and a headless configure take the same path. Launches keep needing a
terminal or a file to repair. Codex's side is in `test_agent_codex.py`.
"""

import json
import tomllib
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from typer.testing import CliRunner

from tests.test_claude_header_mod import WS, _managed_env
from ucode import cli as cli_mod
from ucode import managed_files
from ucode.agents import claude, codex
from ucode.agents.desktop import setup as desktop_setup

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


@pytest.fixture
def isolated_revert(monkeypatch):
    """Exclude unrelated agent cleanup from managed-file reporting scenarios."""
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: None)
    monkeypatch.setattr(codex, "codex_managed_config_path", lambda: None)
    monkeypatch.setattr(desktop_setup, "revert_desktop", lambda: "unchanged")
    monkeypatch.setattr(cli_mod, "revert_mcp_configs", lambda state: {})
    monkeypatch.setattr(cli_mod, "restore_file", lambda *args: False)
    monkeypatch.setattr(cli_mod, "revert_legacy_shared_config", lambda: False)
    monkeypatch.setattr(cli_mod.copilot_agent, "scrub_stored_credentials", lambda: None)
    monkeypatch.setattr(cli_mod.copilot_agent, "sweep_session_registries", lambda: None)


@pytest.mark.parametrize("tool, display", [("claude", "Claude Code"), ("codex", "Codex")])
def test_revert_cli_reports_managed_file_deletion_failure(
    tmp_path, monkeypatch, isolated_revert, tool, display
):
    path = _write_policy_env(tmp_path, monkeypatch).with_suffix(
        ".json" if tool == "claude" else ".toml"
    )
    monkeypatch.setattr(
        claude if tool == "claude" else codex,
        "_managed_settings_path" if tool == "claude" else "codex_managed_config_path",
        lambda: path,
    )
    managed_files.request_explicit_managed_writes()
    contents = '{"a": 1}' if tool == "claude" else "a = 1\n"
    managed_files.reconcile_managed_file(
        path,
        contents,
        tool=tool,
        display=display,
        owned_paths=[],
        parser=json.loads if tool == "claude" else tomllib.loads,
    )
    backup_dir = managed_files.MANAGED_BACKUP_DIR
    backup = {file.name: file.read_bytes() for file in backup_dir.iterdir()}
    cli_mod.save_state({"workspace": "https://workspace.example"})
    state_before = cli_mod.load_state()

    def denied(_target):
        raise CalledProcessError(1, ["sudo", "-n", "rm"], stderr="sudo: a password is required")

    def reject_write(_target, _text):
        pytest.fail("A failed deletion must not rewrite the managed settings")

    monkeypatch.setattr(managed_files, "_sudo_remove", denied)
    monkeypatch.setattr(managed_files, "_sudo_replace", reject_write)

    result = CliRunner().invoke(cli_mod.app, ["revert"], env={"COLUMNS": "240"})

    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert f"Could not restore {display} managed settings" in output
    assert str(path) in output
    assert "The backup was retained" in output
    assert "run `ug revert` again" in output
    assert path.read_text(encoding="utf-8") == contents
    assert {file.name: file.read_bytes() for file in backup_dir.iterdir()} == backup
    assert cli_mod.load_state() == state_before


@pytest.mark.parametrize("tool, display", [("claude", "Claude Code"), ("codex", "Codex")])
@pytest.mark.parametrize("manifest_present", [False, True], ids=["no-manifest", "no-agent-record"])
def test_revert_cli_explains_untracked_managed_file(
    tmp_path, monkeypatch, isolated_revert, tool, display, manifest_present
):
    path = tmp_path / ("managed-settings.json" if tool == "claude" else "managed_config.toml")
    contents = b'{"apiKeyHelper": "existing-helper"}\n' if tool == "claude" else b'a = "existing"\n'
    path.write_bytes(contents)
    monkeypatch.setattr(
        claude if tool == "claude" else codex,
        "_managed_settings_path" if tool == "claude" else "codex_managed_config_path",
        lambda: path,
    )
    manifest = managed_files.MANAGED_BACKUP_MANIFEST_PATH
    if manifest_present:
        manifest.parent.mkdir()
        manifest.write_text('{"version": 1, "files": {}}', encoding="utf-8")
    cli_mod.save_state({"workspace": WS})

    result = CliRunner().invoke(cli_mod.app, ["revert"], env={"COLUMNS": "240"})

    output = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert f"{display} managed settings at {path} were left unchanged" in output
    assert "no UG backup record was found" in output
    assert "If no longer needed, remove the file manually or ask your administrator" in output
    assert path.read_bytes() == contents
    assert manifest.exists() == manifest_present
    if manifest_present:
        assert json.loads(manifest.read_text()) == {"version": 1, "files": {}}
    assert cli_mod.load_state() == {}


@pytest.mark.parametrize("tool", ["claude", "codex", None], ids=["claude", "codex", "unsupported"])
def test_revert_cli_omits_manual_cleanup_guidance_when_no_managed_file_exists(
    tmp_path, monkeypatch, isolated_revert, tool
):
    if tool is not None:
        monkeypatch.setattr(
            claude if tool == "claude" else codex,
            "_managed_settings_path" if tool == "claude" else "codex_managed_config_path",
            lambda: tmp_path / "absent-managed-settings",
        )

    result = CliRunner().invoke(cli_mod.app, ["revert"])

    assert result.exit_code == 0, result.output
    assert "no UG backup record" not in result.output
    assert "remove the file manually" not in result.output
    assert "Claude Code OS-managed settings: unchanged" in result.output
    assert "Codex OS-managed settings: unchanged" in result.output


@pytest.mark.parametrize("tool, display", [("claude", "Claude Code"), ("codex", "Codex")])
def test_revert_cli_reports_an_uninspectable_managed_path_without_failing_cleanup(
    tmp_path, monkeypatch, isolated_revert, tool, display
):
    path = tmp_path / "locked-directory" / "managed-settings.json"
    monkeypatch.setattr(
        claude if tool == "claude" else codex,
        "_managed_settings_path" if tool == "claude" else "codex_managed_config_path",
        lambda: path,
    )
    real_exists = Path.exists

    def exists(target):
        if target == path:
            raise PermissionError("Permission denied")
        return real_exists(target)

    monkeypatch.setattr(Path, "exists", exists)
    cli_mod.save_state({"workspace": WS})

    result = CliRunner().invoke(cli_mod.app, ["revert"], env={"COLUMNS": "240"})

    output = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert f"Could not inspect {display} managed settings at {path}" in output
    assert "Check this file manually or ask your administrator" in output
    assert cli_mod.load_state() == {}


@pytest.mark.parametrize("tool, display", [("claude", "Claude Code"), ("codex", "Codex")])
def test_revert_cli_does_not_warn_for_a_restored_managed_file(
    tmp_path, monkeypatch, isolated_revert, tool, display
):
    path = _write_policy_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        claude if tool == "claude" else codex,
        "_managed_settings_path" if tool == "claude" else "codex_managed_config_path",
        lambda: path,
    )
    original = '{"a": 1}\n' if tool == "claude" else "a = 1\n"
    changed = '{"a": 2}\n' if tool == "claude" else "a = 2\n"
    path.write_text(original, encoding="utf-8")
    managed_files.request_explicit_managed_writes()
    managed_files.reconcile_managed_file(
        path,
        changed,
        tool=tool,
        display=display,
        owned_paths=[],
        parser=json.loads if tool == "claude" else tomllib.loads,
    )

    result = CliRunner().invoke(cli_mod.app, ["revert"])

    assert result.exit_code == 0, result.output
    assert f"{display} OS-managed settings: restored" in result.output
    assert "no UG backup record" not in result.output
    assert "remove the file manually" not in result.output
    assert path.read_text(encoding="utf-8") == original


def test_revert_cli_explains_untracked_settings_after_relay_conflict(
    tmp_path, monkeypatch, isolated_revert
):
    path, _root, _writes = _managed_env(
        tmp_path,
        monkeypatch,
        {"apiKeyHelper": "existing-helper", "permissions": {"deny": ["WebSearch"]}},
    )
    contents = path.read_bytes()
    state = {"workspace": WS, "codex_models": [], "relayed_proxy_port": 9999}
    with pytest.raises(RuntimeError, match="Claude subscription relay cannot start"):
        claude.write_tool_config(state, MODEL, relayed=True)
    assert not managed_files.MANAGED_BACKUP_MANIFEST_PATH.exists()
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: None)
    cli_mod.save_state(state)

    result = CliRunner().invoke(cli_mod.app, ["revert"], env={"COLUMNS": "240"})

    output = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert str(path) in output
    assert "no UG backup record was found" in output
    assert "remove the file manually" in output
    assert path.read_bytes() == contents
    assert not managed_files.MANAGED_BACKUP_MANIFEST_PATH.exists()
    assert cli_mod.load_state() == {}


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
