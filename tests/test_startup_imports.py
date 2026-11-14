"""Component guards for startup imports and compatible ug/ucode version reporting."""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

from ucode import entrypoint

_LAZY_MODULES = (
    "databricks.sdk",
    "httpx",
    "mcp",
    "websockets",
    "ucode.gateway_proxy",
    "ucode.smart_routing.codex_interposer",
)


def _modules_loaded_after(code: str, modules: tuple[str, ...]) -> tuple[str, list[str]]:
    """Run code in a fresh interpreter and report the guarded imports."""
    probe = f"{code}\nimport json, sys\nprint(json.dumps([m for m in {list(modules)!r} if m in sys.modules]))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    *output, loaded = result.stdout.splitlines()
    return "\n".join(output), json.loads(loaded)


def _write_metadata(root: Path, reported_version: str, distribution: str = "unity_gateway") -> Path:
    metadata = root / f"{distribution}-{reported_version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.4\nName: unity-gateway\nVersion: {reported_version}\n",
        encoding="utf-8",
    )
    return metadata


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_entrypoint_skips_cli_imports(command: str, flag: str) -> None:
    output, loaded = _modules_loaded_after(
        f"import sys\nsys.argv = [{command!r}, {flag!r}]\nfrom ucode.entrypoint import main\nmain()",
        ("ucode.cli", "typer", "ucode.telemetry", "importlib.metadata", *_LAZY_MODULES),
    )

    assert output == version("unity-gateway")
    assert loaded == []


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_output_does_not_establish_cli_health(
    tmp_path: Path, command: str, flag: str
) -> None:
    _write_metadata(tmp_path, "1.2.3")
    source = Path(entrypoint.__file__).resolve().parents[1]
    probe = (
        f"import sys\nsys.path[:0] = {[str(tmp_path), str(source)]!r}\n"
        f"sys.argv = [{command!r}, sys.argv[1]]\n"
        "from ucode.entrypoint import main\nmain()"
    )
    # Disable site packages so metadata survives while startup dependencies are absent.
    command_line = [sys.executable, "-I", "-S", "-c", probe]
    reported = subprocess.run(
        [*command_line, flag],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    health = subprocess.run(
        [*command_line, "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert reported.returncode == 0, reported.stderr
    assert reported.stdout == "1.2.3\n"
    assert reported.stderr == ""
    assert health.returncode != 0
    assert "ModuleNotFoundError: No module named 'typer'" in health.stderr


def test_installed_version_matches_importlib_metadata() -> None:
    assert entrypoint._read_installed_version() == version("unity-gateway")


def test_cli_import_defers_subcommand_specific_modules() -> None:
    _, loaded = _modules_loaded_after("import ucode.cli\nimport ucode.agents.claude", _LAZY_MODULES)

    assert loaded == []


@pytest.mark.parametrize("reported_version", ["1.2.3", "1.2.3.post4", "1.2.3+abc123"])
def test_metadata_reader_preserves_version_suffixes(
    tmp_path, monkeypatch, reported_version
) -> None:
    _write_metadata(tmp_path, reported_version)
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == reported_version


@pytest.mark.parametrize("distribution", ["unity_gateway", "UNITY_GATEWAY", "unity.gateway"])
def test_metadata_reader_normalizes_distribution_name(tmp_path, monkeypatch, distribution) -> None:
    _write_metadata(tmp_path, "1.2.3", distribution)
    _write_metadata(tmp_path, "9.9.9", "unity_gateway_extra")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_uses_first_sys_path_match(tmp_path, monkeypatch) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_metadata(first, "1.2.3")
    _write_metadata(second, "4.5.6")
    monkeypatch.setattr(sys, "path", [str(first), str(second)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_handles_empty_sys_path_entry(tmp_path, monkeypatch) -> None:
    _write_metadata(tmp_path, "1.2.3")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [""])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_skips_missing_sys_path_directory(tmp_path, monkeypatch) -> None:
    _write_metadata(tmp_path, "1.2.3")
    monkeypatch.setattr(sys, "path", [str(tmp_path / "missing"), str(tmp_path)])

    assert entrypoint._read_installed_version() == "1.2.3"


@pytest.mark.parametrize(
    "contents",
    [None, "Name: unity-gateway\nVersion: \n", "Name: unity-gateway\n\nVersion: 1.2.3\n"],
)
def test_metadata_reader_falls_back_for_incomplete_metadata(
    tmp_path, monkeypatch, contents
) -> None:
    metadata = _write_metadata(tmp_path, "1.2.3")
    if contents is None:
        metadata.unlink()
    else:
        metadata.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() is None


def test_metadata_reader_falls_back_for_zip_path_before_directory(tmp_path, monkeypatch) -> None:
    archive = tmp_path / "installed.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(
            "unity_gateway-1.2.3.dist-info/METADATA", "Name: unity-gateway\nVersion: 1.2.3\n"
        )
    directory = tmp_path / "later"
    _write_metadata(directory, "4.5.6")
    monkeypatch.setattr(sys, "path", [str(archive), str(directory)])

    assert entrypoint._read_installed_version() is None
    assert version("unity-gateway") == "1.2.3"


def test_metadata_reader_skips_windows_console_script_launcher(tmp_path, monkeypatch) -> None:
    # Windows console scripts put their launcher (e.g. Scripts\ug.exe) first on sys.path.
    launcher = tmp_path / "ug.exe"
    launcher.write_bytes(b"MZ")
    directory = tmp_path / "site-packages"
    _write_metadata(directory, "1.2.3")
    monkeypatch.setattr(sys, "path", [str(launcher), str(directory)])

    assert entrypoint._read_installed_version() == version("unity-gateway") == "1.2.3"


def test_metadata_reader_falls_back_for_egg_info(tmp_path, monkeypatch) -> None:
    egg = tmp_path / "unity_gateway.egg-info"
    egg.mkdir()
    (egg / "PKG-INFO").write_text("Name: unity-gateway\nVersion: 1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(sys, "path", [str(tmp_path)])

    assert entrypoint._read_installed_version() is None
    assert version("unity-gateway") == "1.2.3"


def _unreadable_cwd() -> str:
    raise PermissionError("denied")


def _unexpected_chdir(path: str) -> None:
    raise AssertionError(f"unexpected chdir to {path}")


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_entrypoint_falls_back_to_telemetry(monkeypatch, capsys, flag) -> None:
    from ucode import telemetry

    monkeypatch.setattr(sys, "argv", ["ug", flag])
    monkeypatch.setattr(entrypoint, "_read_installed_version", lambda: None)
    monkeypatch.setattr(telemetry, "ug_version", lambda: "1.2.3.post4+abc123")
    monkeypatch.setattr(entrypoint.os, "chdir", _unexpected_chdir)

    entrypoint.main()

    assert capsys.readouterr().out == "1.2.3.post4+abc123\n"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--help"],
        ["--dry-run", "--version"],
        ["--version", "foo"],
        ["--version", "--help"],
        ["-V", "claude"],
        ["claude", "--version"],
        ["configure"],
    ],
)
def test_other_arguments_delegate_to_cli_unchanged(monkeypatch, argv) -> None:
    from ucode import cli

    calls = []
    monkeypatch.setattr(sys, "argv", ["ug", *argv])
    # cwd recovery is limited to the apiKeyHelper; even an unreadable cwd is left alone here.
    monkeypatch.setattr(entrypoint.os, "getcwd", _unreadable_cwd)
    monkeypatch.setattr(entrypoint.os, "chdir", _unexpected_chdir)
    monkeypatch.setattr(cli, "main", lambda: calls.append(sys.argv.copy()))
    monkeypatch.setattr(
        entrypoint, "_read_installed_version", lambda: pytest.fail("unexpected version fast path")
    )

    entrypoint.main()

    assert calls == [["ug", *argv]]


class _CwdReadingCliFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import a stand-in ``ucode.cli`` that reads the cwd at import time, as Rich does."""

    def __init__(self) -> None:
        self.import_cwd: str | None = None

    def find_spec(self, fullname, path, target=None):
        if fullname == "ucode.cli":
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        self.import_cwd = os.getcwd()  # raises like Rich's import if the cwd is unreadable
        module.main = lambda: print("token")


def _import_cli_through(monkeypatch, finder: _CwdReadingCliFinder) -> None:
    import ucode

    monkeypatch.delitem(sys.modules, "ucode.cli", raising=False)
    monkeypatch.delattr(ucode, "cli", raising=False)
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])


def test_api_key_helper_keeps_readable_cwd(tmp_path, monkeypatch, capsys) -> None:
    # A project-relative DATABRICKS_BEARER_COMMAND (e.g. ./get-token.sh) resolves against this cwd.
    monkeypatch.chdir(tmp_path)
    finder = _CwdReadingCliFinder()
    _import_cli_through(monkeypatch, finder)
    monkeypatch.setattr(entrypoint.os, "chdir", _unexpected_chdir)
    monkeypatch.setattr(sys, "argv", ["ug", "auth-token"])

    entrypoint.main()

    assert finder.import_cwd == str(tmp_path)
    assert capsys.readouterr() == ("token\n", "")


@pytest.mark.parametrize("failure", [PermissionError("denied"), FileNotFoundError("removed")])
def test_api_key_helper_recovers_unreadable_cwd_before_cli_import(
    tmp_path, monkeypatch, capsys, failure
) -> None:
    home = tmp_path / "home"
    blocked = tmp_path / "blocked"
    home.mkdir()
    blocked.mkdir()
    monkeypatch.chdir(blocked)  # restore the original cwd at teardown
    real_getcwd = os.getcwd

    def getcwd_denied_in_blocked() -> str:
        # Like macOS for a privacy-protected folder: only the blocked cwd is unreadable.
        cwd = real_getcwd()
        if cwd == str(blocked):
            raise failure
        return cwd

    finder = _CwdReadingCliFinder()
    _import_cli_through(monkeypatch, finder)
    # Windows resolves `~` from USERPROFILE, not HOME, so patch the lookup itself.
    monkeypatch.setattr(entrypoint.os.path, "expanduser", lambda _path: str(home))
    monkeypatch.setattr(os, "getcwd", getcwd_denied_in_blocked)
    monkeypatch.setattr(sys, "argv", ["ug", "auth-token"])

    entrypoint.main()

    assert finder.import_cwd == str(home)
    assert capsys.readouterr() == ("token\n", "")


def test_api_key_helper_falls_back_to_temp_when_home_is_unavailable(
    tmp_path, monkeypatch, capsys
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.chdir(tmp_path)
    real_getcwd = os.getcwd
    unreadable = True

    def getcwd_until_moved() -> str:
        if unreadable:
            raise PermissionError("denied")
        return real_getcwd()

    real_chdir = os.chdir
    attempts = []

    def chdir_without_home(path: str) -> None:
        nonlocal unreadable
        attempts.append(path)
        if path != str(temporary):
            raise PermissionError("denied")
        real_chdir(path)
        unreadable = False

    finder = _CwdReadingCliFinder()
    _import_cli_through(monkeypatch, finder)
    monkeypatch.setattr(entrypoint.os.path, "expanduser", lambda _path: str(tmp_path / "home"))
    monkeypatch.setattr(entrypoint.tempfile, "gettempdir", lambda: str(temporary))
    monkeypatch.setattr(os, "getcwd", getcwd_until_moved)
    monkeypatch.setattr(entrypoint.os, "chdir", chdir_without_home)
    monkeypatch.setattr(sys, "argv", ["ug", "auth-token"])

    entrypoint.main()

    assert attempts == [str(tmp_path / "home"), str(temporary)]
    assert finder.import_cwd == str(temporary)
    assert capsys.readouterr() == ("token\n", "")
