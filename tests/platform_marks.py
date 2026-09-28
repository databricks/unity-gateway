"""Skip markers for unit tests that exercise OS features some hosts lack.

CI runs on Linux; these keep the suite green on Windows dev machines without
weakening what the tests assert where the feature exists.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


def _can_create_symlinks() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            (Path(tmp) / "link").symlink_to(tmp, target_is_directory=True)
        except OSError:
            return False
    return True


# Windows only allows symlinks with Developer Mode or admin rights (WinError 1314).
requires_symlinks = pytest.mark.skipif(
    not _can_create_symlinks(),
    reason="creating symlinks needs Developer Mode or admin rights on Windows",
)


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip on Windows, where the POSIX behavior under test does not exist."""
    return pytest.mark.skipif(sys.platform == "win32", reason=reason)
