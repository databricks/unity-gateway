"""Helpers for building the outbound `User-Agent` we attach to agent traffic.

The gateway uses the UA to attribute requests to ucode and to a specific
wrapped agent + version, and the client OS. Format:
`ucode/<ucode_ver> <agent>/<agent_ver> (<os> <os_ver>; <arch>)`, e.g.
`(macos 15.1; arm64)`, `(windows 22631; x86_64)`, `(linux ubuntu-24.04; x86_64)`.
`<os_ver>` is the macOS major.minor, the Windows build number, or the Linux
`ID-VERSION_ID`; `unknown` when unavailable. `<arch>` is the host arch, not the
interpreter's. The leading `ucode/<v> <agent>/<v>` is what the gateway parser
keys on, so the platform suffix must stay last.

The helpers fall back to "unknown" rather than raising — telemetry must
never block a launch.
"""

from __future__ import annotations

import ctypes
import platform
import re
import subprocess
import sys
from functools import cache
from importlib.metadata import PackageNotFoundError, version

from ucode.os_compatibility import subprocess_cross_os

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+[-+0-9A-Za-z.]*")
_UNSAFE_TOKEN_CHARS = re.compile(r"[^a-z0-9_.-]+")
_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
    "armv6l": "arm",
    "armv7l": "arm",
}
_WINDOWS_MACHINES = {0x8664: "x86_64", 0xAA64: "arm64", 0x014C: "x86", 0x01C4: "arm"}


@cache
def ug_version() -> str:
    try:
        return version("unity-gateway")
    except PackageNotFoundError:
        return "unknown"


@cache
def agent_version(binary: str) -> str:
    """Return the agent CLI's reported version, or "unknown" on any failure.

    Spawned at most once per binary per session (cached). Each agent CLI
    formats `--version` differently — we extract the first semver-shaped
    token from stdout (then stderr) so the same parser handles all of them.
    """
    try:
        result = subprocess_cross_os.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"
    for stream in (result.stdout, result.stderr):
        if not stream:
            continue
        match = _SEMVER_RE.search(stream)
        if match:
            return match.group(0)
    return "unknown"


def _safe_token(raw: str) -> str:
    # No commas (Gemini splits custom headers on them), colons or parens: the
    # suffix is parsed back out of managed files.
    return _UNSAFE_TOKEN_CHARS.sub("-", raw.strip().lower()).strip("-") or "unknown"


def normalize_os(platform_name: str) -> str:
    if platform_name == "darwin":
        return "macos"
    if platform_name in ("win32", "cygwin"):
        return "windows"
    if platform_name.startswith("linux"):
        return "linux"
    return _safe_token(platform_name)


def normalize_arch(machine: str) -> str:
    key = machine.strip().lower()
    return _ARCH_ALIASES.get(key) or _safe_token(key)


def _macos_version() -> str:
    return ".".join(platform.mac_ver()[0].split(".")[:2])


def _windows_build() -> str:
    return platform.win32_ver()[1].split(".")[-1]


def _linux_distro_version() -> str:
    release = platform.freedesktop_os_release()
    distro, version_id = release.get("ID", ""), release.get("VERSION_ID", "")
    return f"{distro}-{version_id}" if distro and version_id else distro


def _os_version(os_name: str) -> str:
    probes = {"macos": _macos_version, "windows": _windows_build, "linux": _linux_distro_version}
    probe = probes.get(os_name)
    if probe is None:
        return "unknown"
    try:
        return _safe_token(probe())
    except Exception:  # noqa: BLE001
        return "unknown"


def _macos_translated() -> bool:
    libc = ctypes.CDLL("libc.dylib")
    libc.sysctlbyname.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctlbyname.restype = ctypes.c_int
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    rc = libc.sysctlbyname(
        b"sysctl.proc_translated", ctypes.byref(value), ctypes.byref(size), None, 0
    )
    return rc == 0 and value.value == 1


def _windows_native_arch() -> str | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    is_wow64_process2 = kernel32.IsWow64Process2
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    process_machine, native_machine = wintypes.USHORT(0), wintypes.USHORT(0)
    ok = is_wow64_process2(
        kernel32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)
    )
    return _WINDOWS_MACHINES.get(native_machine.value) if ok else None


def _host_arch() -> str:
    # platform.machine() reports the interpreter's arch, which is x86_64 for an
    # emulated process (Rosetta, Windows-on-ARM) on an arm64 host.
    default = normalize_arch(platform.machine())
    try:
        if sys.platform == "darwin" and _macos_translated():
            return "arm64"
        if sys.platform == "win32":
            return _windows_native_arch() or default
    except Exception:  # noqa: BLE001
        pass
    return default


@cache
def client_platform() -> str:
    """`(<os> <os_ver>; <arch>)` for the machine running ug."""
    try:
        os_name = normalize_os(sys.platform)
        return f"({os_name} {_os_version(os_name)}; {_host_arch()})"
    except Exception:  # noqa: BLE001
        return "(unknown unknown; unknown)"


def ucode_user_agent(ug_ver: str, *agent_tokens: str) -> str:
    """`ucode/<v> [<agent>/<v> ...] (<os> <os_ver>; <arch>)`; agent tokens are `<agent>/<v>`."""
    return " ".join([f"ucode/{ug_ver}", *agent_tokens, client_platform()])
