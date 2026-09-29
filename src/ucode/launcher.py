"""Cross-platform process replacement for launching coding agents."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
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
    """The program an npm ``.cmd`` shim launches, so it can run without ``cmd.exe``.

    A ``.cmd`` runs under ``cmd.exe``, which re-parses the command line: Python's
    ``\\"`` escaping means nothing to it, so an argument such as Codex's
    ``hooks.PreToolUse=[{matcher = "Agent|..."}]`` is split at ``|`` as a pipe.
    Killing the shim also leaves its node child running. Only npm's standard shim
    shapes are recognised; anything else returns None and runs through the shim.
    """
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
    """Return ``argv`` with its program resolved to something Windows can spawn.

    npm installs agent CLIs on Windows as ``.cmd`` shims. ``shutil.which`` finds
    them via ``PATHEXT``, but ``CreateProcess`` only appends ``.exe`` to a bare
    name, so ``subprocess`` raises ``FileNotFoundError`` for ``["npm", ...]`` or
    ``["codex", ...]``. A recognised npm shim is replaced by the program it runs
    (see ``_unwrap_npm_shim``); any other match is used by full path. POSIX
    resolves bare names itself, so ``argv`` is returned unchanged there (and when
    not found, letting the spawn raise its usual error).
    """
    if os.name != "nt" or not argv:
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


def exec_or_spawn(argv: list[str]) -> None:
    """Hand the terminal to ``argv``, then exit with its status.

    On POSIX we ``os.execvp`` — the agent process *replaces* ucode, inheriting
    the controlling terminal cleanly.

    On Windows there is no real ``exec``: ``os.execvp`` spawns a *new* process
    and immediately terminates the parent, so the launching shell resumes its
    prompt and fights the agent for the console. That produces the garbled,
    split-screen input reported in issue #173. Instead we spawn a child, wait
    for it, and propagate its exit code — the same pattern the token-refreshing
    agents (gemini/opencode/copilot/pi) already use.
    """
    if os.name != "nt":
        os.execvp(argv[0], argv)
        return  # unreachable on POSIX; keeps type-checkers happy

    proc = popen(argv)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C is delivered to the whole console group; let the child handle
        # it and report its own exit code rather than racing it.
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    sys.exit(returncode)
