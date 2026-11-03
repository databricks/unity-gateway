"""Open a URL in the user's browser on every supported OS."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from ucode.os_compatibility import subprocess_cross_os

__all__ = ["open_in_browser"]

_OPEN_TIMEOUT_SECONDS = 10
# Console browsers webbrowser may pick on a headless box; they "succeed" without showing anything.
_TEXT_BROWSERS = frozenset({"www-browser", "lynx", "links", "elinks", "w3m"})


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the default browser; return whether a browser accepted it.

    ``webbrowser`` runs in a child process with no stdin so a terminal browser (lynx, w3m) picked on
    a headless Linux box cannot take over the user's terminal. Headless Linux (SSH, no display, no
    forwarding ``BROWSER``) returns False up front, since a console browser would falsely report
    success. WSL has no Linux browser by default, so it falls back to ``wslview``.
    """
    if _is_wsl():
        if _open_with_webbrowser(url):
            return True
        wslview = shutil.which("wslview")
        return bool(wslview) and _run_quietly([wslview, url])
    if sys.platform == "linux" and not _linux_can_show_browser():
        return False
    return _open_with_webbrowser(url)


def _linux_can_show_browser() -> bool:
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    # Remote editors (e.g. VS Code over SSH) set BROWSER to a helper that opens the local browser.
    browser = os.environ.get("BROWSER", "").split(os.pathsep)[0].strip()
    return bool(browser) and os.path.basename(browser.split()[0]) not in _TEXT_BROWSERS


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
