"""Cross-platform process replacement for launching coding agents."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping

from ucode.child_env import resolve_agent_argv


def exec_or_spawn(argv: list[str], *, env: Mapping[str, str] | None = None) -> None:
    """Hand the terminal to ``argv``, then exit with its status.

    On POSIX the agent process replaces ucode, inheriting the controlling
    terminal cleanly. An explicit environment uses a resolved executable.

    On Windows there is no real ``exec``: ``os.execvp`` spawns a *new* process
    and immediately terminates the parent, so the launching shell resumes its
    prompt and fights the agent for the console. That produces the garbled,
    split-screen input reported in issue #173. Instead we spawn a child, wait
    for it, and propagate its exit code, the same pattern the token-refreshing
    agents (gemini/opencode/copilot/pi) already use.
    """
    if env is not None:
        argv = resolve_agent_argv(argv)
    if os.name != "nt":
        if env is None:
            os.execvp(argv[0], argv)
        else:
            os.execve(argv[0], argv, dict(env))
        return  # unreachable on POSIX; keeps type-checkers happy

    proc = subprocess.Popen(argv, **({"env": dict(env)} if env is not None else {}))
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C is delivered to the whole console group; let the child handle
        # it and report its own exit code rather than racing it.
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    sys.exit(returncode)
