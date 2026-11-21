"""Open a URL in the user's browser on Windows, macOS, and Linux."""

from __future__ import annotations

import subprocess
import sys

from ucode.os_compatibility import subprocess_cross_os

__all__ = ["open_in_browser"]

_OPEN_TIMEOUT_SECONDS = 10


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the default browser; return whether a browser accepted it.

    ``webbrowser`` runs in a child process with no stdin so a console browser (lynx, w3m) picked on
    a headless Linux box cannot take over the user's terminal.
    """
    try:
        result = subprocess_cross_os.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)",
                url,
            ],
            check=False,
            timeout=_OPEN_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
