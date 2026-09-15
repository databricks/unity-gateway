"""Smoke tests for the installed ``ug`` and ``ucode`` console scripts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@pytest.mark.parametrize("command", ["ug", "ucode"])
@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_installed_console_script_runs_with_its_invoked_name(command: str, flag: str) -> None:
    """Both scripts installed by ``uv run pytest`` execute the same CLI successfully."""
    bin_dir = Path(sys.executable).parent
    script = shutil.which(command, path=str(bin_dir))
    assert script is not None, f"{command} was not installed in {bin_dir}"

    result = subprocess.run(
        [script, flag],
        cwd=Path(__file__).parent.parent,
        env={**os.environ, "NO_COLOR": "1"},
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    if flag == "--version":
        assert _ANSI_RE.sub("", result.stdout).strip() == version("unity-gateway")
    else:
        assert f"Usage: {command} " in _ANSI_RE.sub("", output)
