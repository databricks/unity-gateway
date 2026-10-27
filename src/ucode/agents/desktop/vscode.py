"""Copilot Chat in VS Code: the loopback bridge its Custom Endpoint models call.

VS Code's Custom Endpoint models carry only static request headers, so they present a
long-lived bridge secret to `ug vscode-bridge`, which forwards each request to the
workspace AI Gateway with a fresh Databricks token.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ucode import config_io, managed_config
from ucode import state as state_module
from ucode.agents import copilot
from ucode.config_io import APP_DIR
from ucode.custom_oauth import launch_token
from ucode.databricks import apply_pat_environment
from ucode.managed_resolve import resolve_state
from ucode.telemetry import ucode_user_agent, ug_version

BRIDGE_STATE_PATH = APP_DIR / "vscode-bridge.json"
BRIDGE_LOG_PATH = APP_DIR / "logs" / "vscode-bridge.log"
_SECRET_KEY = "secret"
_COPILOT_CHAT_PRODUCT = re.compile(r"(?i)(?:^|\s)GitHubCopilotChat/([^\s(),;]+)")
_COPILOT_CHAT_AGENT = "githubcopilotchat"


def bridge_secret() -> str:
    """The secret VS Code presents to the bridge, created on first use."""
    secret = config_io.read_json_safe(BRIDGE_STATE_PATH).get(_SECRET_KEY)
    if isinstance(secret, str) and secret:
        return secret
    secret = secrets.token_urlsafe(32)
    # The temp file atomic_write_json renames into place is created at mode 0600.
    config_io.atomic_write_json(BRIDGE_STATE_PATH, {_SECRET_KEY: secret})
    return secret


def bridge_user_agent(ug: str) -> Callable[[str | None], str]:
    """The forwarded User-Agent: ug's, attributing the request to Copilot Chat's version."""

    def user_agent(client: str | None) -> str:
        match = _COPILOT_CHAT_PRODUCT.search(client or "")
        agent = f"{_COPILOT_CHAT_AGENT}/{match.group(1)}" if match else _COPILOT_CHAT_AGENT
        return ucode_user_agent(ug, agent)

    return user_agent


def _log_refresh_failure(exc: BaseException) -> None:
    # A login service has no terminal, so failures go to a file instead of stderr.
    try:
        BRIDGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BRIDGE_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} token refresh failed: {exc}\n")
    except OSError:
        pass


def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _auth_settings(state: dict) -> tuple[object, ...]:
    return state.get("profile"), state.get("custom_oauth"), state.get("use_pat")


class _WorkspaceState:
    """The workspace's saved state with its cached managed config layered on, as a Copilot
    launch resolves it. Re-read when `state.json` or a managed-config file changes, so
    `ug configure` and managed updates reach a running bridge. While the workspace is missing
    from `state.json` (e.g. after `ug revert`), the last state read is kept."""

    def __init__(self, host: str) -> None:
        self._host = host
        self._lock = threading.Lock()
        self._stamp: tuple[tuple[int, int] | None, ...] | None = None
        self._state: dict = {}
        self.on_auth_change: Callable[[], None] | None = None
        if not self.current():
            raise RuntimeError(f"No saved configuration for {host}. Run `ug configure` first.")

    def current(self) -> dict:
        auth_changed = False
        with self._lock:
            stamp = tuple(
                _file_stamp(path)
                for path in (
                    state_module.STATE_PATH,
                    managed_config.MANAGED_CONFIG_PATH,
                    managed_config.USER_MANAGED_CONFIG_PATH,
                )
            )
            if stamp != self._stamp:
                self._stamp = stamp
                saved = state_module.load_full_state().get("workspaces", {}).get(self._host)
                if saved:
                    apply_pat_environment(saved)
                    managed = managed_config.load_managed_state(self._host)
                    resolved = resolve_state(managed, saved, "copilot") if managed else saved
                    auth_changed = bool(self._state) and (
                        _auth_settings(resolved) != _auth_settings(self._state)
                    )
                    self._state = resolved
            state = self._state
        if auth_changed and self.on_auth_change is not None:
            self.on_auth_change()
        return state


def run_bridge(host: str, port: int) -> None:
    """Serve the bridge for ``host`` on ``port`` until the process is stopped."""
    from ucode import gateway_proxy

    workspace = _WorkspaceState(host)
    server, cache, _client = gateway_proxy.start_gateway_auth_proxy(
        host,
        lambda force: launch_token(workspace.current(), host, force_refresh=force),
        session_secret=bridge_secret(),
        added_headers={},
        headers_provider=lambda: copilot.gateway_headers(workspace.current()),
        sign_in_hint=f"Run `databricks auth login --host {host}`, then retry.",
        on_refresh_failure=_log_refresh_failure,
        # The same Copilot BYOK client as the CLI, so the same field fix applies.
        dropped_chat_completions_fields=("reasoning_effort",),
        port=port,
        user_agent=bridge_user_agent(ug_version()),
    )

    def refresh_token() -> None:
        # A token minted under the previous profile would otherwise serve until it expires.
        try:
            cache.refresh()
        except RuntimeError as exc:
            _log_refresh_failure(exc)

    workspace.on_auth_change = refresh_token
    # The service manager stops this with SIGTERM, which exits without cleanup.
    server.serve_forever()
