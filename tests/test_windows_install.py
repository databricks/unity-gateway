import hashlib
import io
import os
import tarfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ucode.agents as agents_mod
from ucode.agents import windows_install

CLAUDE_EXE = b"claude binary"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _codex_package(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Pretend to be a non-elevated Windows user whose downloads come from ``files``."""
    files: dict[str, bytes] = {}
    added: list[Path] = []
    monkeypatch.setattr(windows_install, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_install, "is_admin", lambda: False)
    monkeypatch.setattr(windows_install, "_arm64", lambda: True)
    monkeypatch.setattr(windows_install, "windows_program_files_dir", lambda: tmp_path / "pf")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(windows_install, "_get_text", lambda url: files[url].decode().strip())
    monkeypatch.setattr(
        windows_install, "_download", lambda url, path: path.write_bytes(files[url])
    )
    monkeypatch.setattr(windows_install, "_add_to_path", added.append)
    return types.SimpleNamespace(files=files, added=added, root=tmp_path / "local" / "UnityGateway")


def _serve_claude(files, checksum=None):
    base = f"{windows_install.CLAUDE_RELEASES}/2.1.322"
    files[f"{windows_install.CLAUDE_RELEASES}/latest"] = b"2.1.322\n"
    manifest = (
        f'{{"platforms": {{"win32-arm64": {{"checksum": "{checksum or _sha(CLAUDE_EXE)}"}}}}}}'
    )
    files[f"{base}/manifest.json"] = manifest.encode()
    files[f"{base}/win32-arm64/claude.exe"] = CLAUDE_EXE


def _serve_codex(files, package_files):
    package = _codex_package(package_files)
    name = "codex-package-aarch64-pc-windows-msvc.tar.gz"
    files[f"{windows_install.CODEX_RELEASES}/{name}"] = package
    files[f"{windows_install.CODEX_RELEASES}/codex-package_SHA256SUMS"] = (
        f"{'0' * 64}  codex-package-x86_64-pc-windows-msvc.tar.gz\n{_sha(package)} *{name}\n"
    ).encode()


def test_no_native_installer_off_windows():
    assert windows_install.installer("claude") is None
    assert windows_install.installed_binary("codex") is None


def test_only_claude_and_codex_install_natively(windows):
    assert windows_install.installer("claude") is windows_install.install_claude
    assert windows_install.installer("codex") is windows_install.install_codex
    assert windows_install.installer("opencode") is None


def test_install_claude_checks_manifest_checksum(windows):
    _serve_claude(windows.files)

    windows_install.install_claude()

    assert (windows.root / "bin" / "claude.exe").read_bytes() == CLAUDE_EXE
    assert windows.added == [windows.root / "bin"]
    assert [p.name for p in (windows.root / "bin").iterdir()] == ["claude.exe"]


def test_install_claude_rejects_checksum_mismatch(windows):
    _serve_claude(windows.files, checksum="0" * 64)

    with pytest.raises(RuntimeError, match="Checksum mismatch for claude.exe"):
        windows_install.install_claude()

    assert list((windows.root / "bin").iterdir()) == []
    assert windows.added == []


def test_install_claude_rejects_unexpected_version(windows):
    windows.files[f"{windows_install.CLAUDE_RELEASES}/latest"] = b"../../evil"

    with pytest.raises(RuntimeError, match="Unexpected Claude Code release version"):
        windows_install.install_claude()


def test_install_codex_keeps_package_layout_and_replaces_old_install(windows):
    _serve_codex(
        windows.files,
        {"bin/codex.exe": b"codex", "codex-resources/runner.exe": b"r", "codex-path/rg.exe": b"g"},
    )
    stale = windows.root / "codex" / "bin" / "stale.dll"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    windows_install.install_codex()

    codex_dir = windows.root / "codex"
    assert (codex_dir / "bin" / "codex.exe").read_bytes() == b"codex"
    assert (codex_dir / "codex-resources" / "runner.exe").is_file()
    assert (codex_dir / "codex-path" / "rg.exe").is_file()
    assert not stale.exists()
    assert windows.added == [codex_dir / "bin"]
    assert sorted(p.name for p in windows.root.iterdir()) == ["codex"]


def test_install_codex_rejects_package_without_binary(windows):
    _serve_codex(windows.files, {"README.md": b"x"})

    with pytest.raises(RuntimeError, match="has no bin/codex.exe"):
        windows_install.install_codex()

    assert not (windows.root / "codex").exists()


def test_elevated_install_goes_under_program_files(windows, monkeypatch, tmp_path):
    monkeypatch.setattr(windows_install, "is_admin", lambda: True)
    _serve_claude(windows.files)

    windows_install.install_claude()

    assert (tmp_path / "pf" / "UnityGateway" / "bin" / "claude.exe").is_file()


def test_machine_wide_install_is_found_from_a_standard_user(windows, monkeypatch, tmp_path):
    codex = tmp_path / "pf" / "UnityGateway" / "codex" / "bin" / "codex.exe"
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b"codex")
    monkeypatch.setenv("PATH", "/usr/bin")

    windows_install.add_installed_to_path("codex")

    assert windows_install.installed_binary("codex") == str(codex)
    assert os.environ["PATH"].split(os.pathsep) == [str(codex.parent), "/usr/bin"]
    windows_install.add_installed_to_path("codex")
    assert os.environ["PATH"].split(os.pathsep) == [str(codex.parent), "/usr/bin"]


def test_missing_agent_installs_natively_without_npm(windows, monkeypatch):
    installed = []
    monkeypatch.setattr(agents_mod.shutil, "which", lambda b: None)

    def _install():
        installed.append(True)
        exe = windows.root / "codex" / "bin" / "codex.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"codex")

    monkeypatch.setattr(windows_install, "install_codex", _install)
    monkeypatch.setattr(agents_mod, "_minimum_version_error", lambda tool: None)
    run = MagicMock()
    monkeypatch.setattr(agents_mod.subprocess_cross_os, "run", run)

    assert agents_mod.install_tool_binary("codex", strict=False) is True
    assert installed == [True]
    run.assert_not_called()


def test_native_install_failure_continues_without_agent(windows, monkeypatch, capsys):
    monkeypatch.setattr(agents_mod.shutil, "which", lambda b: None)

    def _fail():
        raise OSError("no network")

    monkeypatch.setattr(windows_install, "install_claude", _fail)

    assert agents_mod.install_tool_binary("claude", strict=False) is False
    assert "Failed to install Claude Code automatically: no network" in capsys.readouterr().out
