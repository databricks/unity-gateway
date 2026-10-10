"""Cross-platform process replacement for launching coding agents."""

from __future__ import annotations

import os
import signal
import sys

from ucode import config_io, skills
from ucode.constants import ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing.config import resolve_environment
from ucode.smart_routing.session_env import start_session


def prepare_agent_launch(*, agent: str, routing_enabled: bool) -> None:
    """Install the requested workflow and create a fresh session before native launch."""
    source = resolve_environment(agent=agent)
    orchestration_enabled = source.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"
    if orchestration_enabled:
        # Do not silently launch without a requested workflow.
        skills.install_skill(
            skills.SMART_ROUTER_ORCHESTRATOR_SKILL, agent, config_io.APP_DIR.parent
        )
    if orchestration_enabled or routing_enabled:
        start_session()


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

    proc = subprocess_cross_os.popen(argv)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C is delivered to the whole console group; let the child handle
        # it and report its own exit code rather than racing it.
        proc.send_signal(signal.SIGINT)
        returncode = proc.wait()
    sys.exit(returncode)
