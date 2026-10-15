"""Claude Code status line for smart-routed launches: the user's line plus the Smart Router state."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ucode.config_io import read_json_safe
from ucode.constants import (
    AGENT_CLAUDE,
    ENABLE_SMART_ROUTING_ENV_VAR,
    ENABLE_SUBAGENT_ROUTING_ENV_VAR,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing.session_env import effective_environment

MODULE = "ucode.smart_routing.status_line"
LABEL = "smart router"
SEPARATOR = " │ "
_ON = "\x1b[1;30;42m ON \x1b[0m"
_OFF = "\x1b[1;30;100m OFF \x1b[0m"
_BASE_TIMEOUT_SECONDS = 10
# Claude re-runs a status line on usage, model, and mode changes, not after a slash command,
# so a refresh interval is what shows a /smart-router toggle.
REFRESH_INTERVAL_SECONDS = 5


def segment(env: Mapping[str, str] | None = None) -> str:
    """The ON/OFF pill, read the way the routing hooks read the session's controls."""
    effective = effective_environment(env, agent=AGENT_CLAUDE)
    values = (
        effective.get(ENABLE_SMART_ROUTING_ENV_VAR),
        effective.get(ENABLE_SUBAGENT_ROUTING_ENV_VAR),
    )
    return f"{LABEL} {_ON if '1' in values else _OFF}"


def effective_status_line(settings: Mapping, settings_paths: Sequence[Path]) -> dict | None:
    """The command status line Claude would otherwise show, in settings precedence order."""
    for source in (settings, *(read_json_safe(path) for path in settings_paths)):
        status_line = source.get("statusLine") if isinstance(source, Mapping) else None
        if isinstance(status_line, Mapping):
            command = status_line.get("command")
            if (
                status_line.get("type") == "command"
                and isinstance(command, str)
                and command.strip()
            ):
                # Never wrap UG's own line, which would show the state twice.
                return None if MODULE in command else dict(status_line)
            # A non-command status line still wins over lower-precedence files.
            return None
    return None


def launch_setting(python: str, base: Mapping | None) -> dict:
    """A statusLine setting that runs the user's command, then appends the pill."""
    argv = [python, "-m", MODULE]
    if base is not None:
        argv.append(base["command"])
    join = subprocess.list2cmdline if os.name == "nt" else shlex.join
    setting = {
        "type": "command",
        "command": join(argv),
        "refreshInterval": REFRESH_INTERVAL_SECONDS,
    }
    if base is not None:
        if "padding" in base:
            setting["padding"] = base["padding"]
        interval = base.get("refreshInterval")
        if isinstance(interval, int | float) and not isinstance(interval, bool) and interval > 0:
            setting["refreshInterval"] = min(interval, REFRESH_INTERVAL_SECONDS)
    return setting


def _base_line(command: str, payload: str) -> str:
    try:
        result = subprocess_cross_os.run(
            command,
            shell=True,
            input=payload,
            capture_output=True,
            text=True,
            timeout=_BASE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else ""


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    payload = sys.stdin.read()
    base = _base_line(args[0], payload) if args else ""
    pill = segment()
    print(f"{base}{SEPARATOR}{pill}" if base else pill)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
