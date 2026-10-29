"""Shared pieces of launching an agent behind a per-session gateway-auth proxy.

Copilot, Antigravity, and Grok hold only a session secret; `gateway_proxy.start_gateway_auth_proxy`
forwards their requests with a fresh Databricks token. These helpers supply that proxy's token and
failure log and wait for the agent, which owns the terminal, to exit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ucode.custom_oauth import launch_token


def token_provider(state: dict) -> Callable[[bool], str]:
    """The proxy's token source: the launch's Databricks token, refreshed on request."""
    workspace = state["workspace"]
    return lambda force_refresh: launch_token(state, workspace, force_refresh=force_refresh)


def refresh_failure_logger(path: Path) -> Callable[[BaseException], None]:
    """Log token-refresh failures to ``path``: the agent owns the terminal, not stderr."""

    def log_failure(exc: BaseException) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} token refresh failed: {exc}\n")
        except OSError:
            pass

    return log_failure


class ChildProcess(Protocol):
    def wait(self) -> int: ...


def wait_for_exit(proc: ChildProcess) -> int:
    """Wait for the agent, keeping the proxy up through the Ctrl-C the terminal also sends it."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue
