"""Helpers for building the outbound `User-Agent` we attach to agent traffic.

The gateway uses the UA to attribute requests to ucode and to a specific
wrapped agent + version, and the client OS. Format:
`ucode/<ucode_ver> <agent>/<agent_ver> (<os>; <arch>)`. The leading
`ucode/<v> <agent>/<v>` is what the gateway parser keys on, so the platform
suffix must stay last.

The helpers fall back to "unknown" rather than raising — telemetry must
never block a launch.
"""

from __future__ import annotations

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
}


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


@cache
def client_platform() -> str:
    """`(<os>; <arch>)` for the machine running ug."""
    try:
        return f"({normalize_os(sys.platform)}; {normalize_arch(platform.machine())})"
    except Exception:  # noqa: BLE001
        return "(unknown; unknown)"


def ucode_user_agent(ug_ver: str, agent: str, agent_ver: str) -> str:
    return f"ucode/{ug_ver} {agent}/{agent_ver} {client_platform()}"
