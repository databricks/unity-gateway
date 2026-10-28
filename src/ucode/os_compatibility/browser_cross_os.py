"""Open a URL in the user's browser on every supported OS."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ucode.os_compatibility import subprocess_cross_os

__all__ = ["open_in_browser"]

_OPEN_TIMEOUT_SECONDS = 10


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the default browser; return whether a browser accepted it.

    ``webbrowser`` runs in a child process with no stdin so a terminal browser (lynx, w3m) picked on
    a headless Linux box cannot take over the user's terminal. WSL has no Linux browser by default,
    so it falls back to ``wslview`` to reach the Windows one.
    """
    if _open_with_webbrowser(url):
        return True
    wslview = shutil.which("wslview") if _is_wsl() else None
    return bool(wslview) and _run_quietly([wslview, url])


def _open_with_webbrowser(url: str) -> bool:
    return _run_quietly(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)",
            url,
        ]
    )


def _run_quietly(args: list[str]) -> bool:
    try:
        result = subprocess_cross_os.run(
            args,
            check=False,
            timeout=_OPEN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _is_wsl() -> bool:
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as version:
            return "microsoft" in version.read().lower()
    except OSError:
        return False
