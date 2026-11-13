"""Native Claude Code and Codex installs on Windows, so ug doesn't need npm there.

Downloads from the same release sources as ``scripts/mdm/macos-provision.sh`` and checks the
published SHA-256 sums. An elevated run (for example an MDM script running as
SYSTEM) installs under Program Files for every user; otherwise under the user's LocalAppData.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from ucode.os_compatibility.system_cross_os import is_admin, windows_program_files_dir

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"
CODEX_RELEASES = "https://github.com/openai/codex/releases/latest/download"
_TIMEOUT_SECONDS = 300
_MACHINE_ENVIRONMENT_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
# Directory under the install root, and the binary in it
_LAYOUT = {"claude": (Path("bin"), "claude.exe"), "codex": (Path("codex", "bin"), "codex.exe")}


def installer(tool: str) -> Callable[[], None] | None:
    """The native installer for ``tool`` on this platform, or ``None`` to fall back to npm."""
    if sys.platform != "win32":
        return None
    return {"claude": install_claude, "codex": install_codex}.get(tool)


def installed_binary(tool: str) -> str | None:
    """A binary an earlier native install left, whether or not this process's PATH has it."""
    if sys.platform != "win32" or tool not in _LAYOUT:
        return None
    subdir, exe = _LAYOUT[tool]
    for root in _roots():
        candidate = root / subdir / exe
        if candidate.is_file():
            return str(candidate)
    return None


def add_installed_to_path(tool: str) -> None:
    """Put an earlier native install on this process's PATH; shells opened before it lack it."""
    binary = installed_binary(tool)
    if binary:
        _prepend_process_path(Path(binary).parent)


def install_claude() -> None:
    platform_name = "win32-arm64" if _arm64() else "win32-x64"
    version = _get_text(f"{CLAUDE_RELEASES}/latest")
    if not re.fullmatch(r"\d+\.\d+\.\d+[\w.+-]*", version):
        raise RuntimeError(f"Unexpected Claude Code release version: {version[:40]!r}")
    base = f"{CLAUDE_RELEASES}/{version}"
    try:
        checksum = json.loads(_get_text(f"{base}/manifest.json"))["platforms"][platform_name][
            "checksum"
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"No Claude Code {version} checksum for {platform_name}") from exc
    directory = _target_root() / _LAYOUT["claude"][0]
    with _staging_dir(directory) as staging:
        exe = staging / "claude.exe"
        _download(f"{base}/{platform_name}/claude.exe", exe)
        _verify_sha256(exe, checksum)
        os.replace(exe, directory / "claude.exe")
    _add_to_path(directory)


def install_codex() -> None:
    target = "aarch64-pc-windows-msvc" if _arm64() else "x86_64-pc-windows-msvc"
    package = f"codex-package-{target}.tar.gz"
    root = _target_root()
    with _staging_dir(root) as staging:
        archive = staging / package
        sums = _get_text(f"{CODEX_RELEASES}/codex-package_SHA256SUMS")
        _download(f"{CODEX_RELEASES}/{package}", archive)
        _verify_sha256(archive, _sha256_for(sums, package))
        # Keep the package layout: codex.exe finds its resources and bundled tools beside bin
        unpacked = staging / "codex"
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")
        if not (unpacked / "bin" / "codex.exe").is_file():
            raise RuntimeError(f"{package} has no bin/codex.exe")
        destination = root / "codex"
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(unpacked, destination)
    _add_to_path(root / _LAYOUT["codex"][0])


def _roots() -> list[Path]:
    roots = [windows_program_files_dir() / "UnityGateway"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "UnityGateway")
    return roots


def _target_root() -> Path:
    if is_admin():
        return _roots()[0]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set")
    return Path(local_app_data) / "UnityGateway"


class _staging_dir:
    """A scratch directory beside the destination, removed on exit.

    Not ``tempfile``: on Windows its 0o700 directories get an owner-only ACL, which a file
    moved out of them keeps, so other users couldn't run a machine-wide install.
    """

    def __init__(self, parent: Path) -> None:
        self.path = parent / f".tmp-{uuid.uuid4().hex}"

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True)
        return self.path

    def __exit__(self, *exc: object) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _arm64() -> bool:
    # x64 Python under emulation on ARM64 reports AMD64; the registry has the native architecture
    arch = platform.machine()
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENVIRONMENT_KEY) as key:
                arch, _ = winreg.QueryValueEx(key, "PROCESSOR_ARCHITECTURE")
        except OSError:
            pass
    return str(arch).upper() == "ARM64"


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read().decode("utf-8").strip()


def _download(url: str, path: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response,  # noqa: S310
        path.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)


def _verify_sha256(path: Path, expected: str) -> None:
    with path.open("rb") as f:
        actual = hashlib.file_digest(f, "sha256").hexdigest()
    if actual != expected.strip().lower():
        raise RuntimeError(f"Checksum mismatch for {path.name}")


def _sha256_for(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == name:
            return parts[0]
    raise RuntimeError(f"No checksum for {name}")


def _prepend_process_path(directory: Path) -> None:
    entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if e]
    if not any(_same_dir(e, directory) for e in entries):
        os.environ["PATH"] = os.pathsep.join([str(directory), *entries])


def _same_dir(entry: str, directory: Path) -> bool:
    expanded = os.path.expandvars(entry).rstrip("\\/")
    return os.path.normcase(expanded) == os.path.normcase(str(directory))


def _add_to_path(directory: Path) -> None:
    """Add ``directory`` to the machine PATH (elevated) or user PATH, and to this process's."""
    _prepend_process_path(directory)
    if sys.platform != "win32":
        return
    import winreg

    if is_admin():
        root, subkey = winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENVIRONMENT_KEY
    else:
        root, subkey = winreg.HKEY_CURRENT_USER, "Environment"
    with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        entries = [e for e in str(value).split(";") if e]
        if any(_same_dir(e, directory) for e in entries):
            return
        winreg.SetValueEx(key, "Path", 0, kind, ";".join([*entries, str(directory)]))
    _broadcast_environment_change()


def _broadcast_environment_change() -> None:
    # Lets Explorer pick up the new PATH, so terminals it starts from now on see it
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    result = wintypes.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
    )
