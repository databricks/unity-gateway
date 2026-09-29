"""Subprocess helpers for platform-specific command resolution."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Launch lines of npm's cmd-shim (the last line containing `%*`). A JS entry point
# run by node:   ... & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
# or a bundled native binary:   "%dp0%\node_modules\opencode-ai\bin\opencode.exe"   %*
# Older shims spell the directory `%~dp0` and name `node` / `"%~dp0\node.exe"` directly.
_SHIM_DIR = r'"%~?dp0%?\\'
_NPM_SHIM_EXE = re.compile(_SHIM_DIR + r'(?P<target>[^"]+\.exe)"\s+%\*', re.IGNORECASE)
_NPM_SHIM_NODE_SCRIPT = re.compile(
    r'(?:^|[&\s])(?:"%_prog%"|node|'
    + _SHIM_DIR
    + r'node\.exe")\s+'
    + _SHIM_DIR
    + r'(?P<target>[^"]+\.[cm]?js)"\s+%\*$',
    re.IGNORECASE,
)


def _unwrap_npm_shim(shim: str) -> list[str] | None:
    """Return direct argv to bypass cmd.exe argument re-parsing for npm shims."""
    try:
        lines = Path(shim).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    launch = next((line.strip() for line in reversed(lines) if "%*" in line), "")
    shim_dir = Path(shim).parent

    def in_shim_dir(relative: str) -> Path:
        return shim_dir.joinpath(*relative.split("\\"))

    if match := _NPM_SHIM_EXE.fullmatch(launch):
        target = in_shim_dir(match["target"])
        return [str(target)] if target.is_file() else None
    if match := _NPM_SHIM_NODE_SCRIPT.search(launch):
        script = in_shim_dir(match["target"])
        bundled_node = shim_dir / "node.exe"
        node = str(bundled_node) if bundled_node.is_file() else shutil.which("node")
        return [node, str(script)] if node and script.is_file() else None
    return None


def resolve_command(argv: list[str]) -> list[str]:
    if os.name != "nt" or not argv:
        # POSIX resolves commands itself; retain the original argv unchanged.
        return argv
    resolved = shutil.which(argv[0])
    if not resolved:
        return argv
    if resolved.lower().endswith(".cmd") and (program := _unwrap_npm_shim(resolved)):
        return [*program, *argv[1:]]
    return [resolved, *argv[1:]]


def _resolved_args(
    args: list[str] | str, *, shell: bool = False, executable: str | None = None
) -> list[str] | str:
    """Leave caller-controlled shell and executable interpretation unchanged."""
    if isinstance(args, list) and not shell and executable is None:
        return resolve_command(args)
    return args


def run(args: list[str] | str, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Like subprocess.run, with automatic Windows resolution for list commands."""
    return subprocess.run(
        _resolved_args(
            args,
            shell=kwargs.get("shell", False),
            executable=kwargs.get("executable"),
        ),
        **kwargs,
    )


def popen(args: list[str] | str, **kwargs: Any) -> subprocess.Popen[Any]:
    """Like subprocess.Popen, with automatic Windows resolution for list commands."""
    return subprocess.Popen(
        _resolved_args(
            args,
            shell=kwargs.get("shell", False),
            executable=kwargs.get("executable"),
        ),
        **kwargs,
    )
