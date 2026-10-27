"""Copilot Chat in VS Code: the loopback bridge its Custom Endpoint models call.

VS Code's Custom Endpoint models carry only static request headers, so they present a
long-lived bridge secret to `ug vscode-bridge`, which forwards each request to the
workspace AI Gateway with a fresh Databricks token.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable

from ucode import config_io
from ucode.agents import copilot
from ucode.config_io import APP_DIR
from ucode.custom_oauth import launch_token
from ucode.databricks import apply_pat_environment
from ucode.state import load_full_state
from ucode.telemetry import ucode_user_agent, ug_version

BRIDGE_STATE_PATH = APP_DIR / "vscode-bridge.json"
BRIDGE_LOG_PATH = APP_DIR / "logs" / "vscode-bridge.log"
_SECRET_KEY = "secret"
_COPILOT_CHAT_PRODUCT = re.compile(r"(?i)(?:^|\s)GitHubCopilotChat/([^\s(),;]+)")
_COPILOT_CHAT_AGENT = "githubcopilotchat"


def bridge_secret() -> str:
    """The secret VS Code presents to the bridge, created on first use."""
    secret = config_io.read_json_safe(BRIDGE_STATE_PATH).get(_SECRET_KEY)
    return secret if isinstance(secret, str) and secret else rotate_bridge_secret()


def rotate_bridge_secret() -> str:
    """Replace the bridge secret and return it. A running bridge keeps the old one until it
    restarts."""
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


def run_bridge(host: str, port: int) -> None:
    """Serve the bridge for ``host`` on ``port`` until the process is stopped."""
    from ucode import gateway_proxy

    state = load_full_state().get("workspaces", {}).get(host)
    if not state:
        raise RuntimeError(f"No saved configuration for {host}. Run `ug configure` first.")
    apply_pat_environment(state)
    server, _cache, _client = gateway_proxy.start_gateway_auth_proxy(
        host,
        lambda force: launch_token(state, host, force_refresh=force),
        session_secret=bridge_secret(),
        added_headers=copilot.gateway_headers(state),
        sign_in_hint=f"Run `databricks auth login --host {host}`, then retry.",
        on_refresh_failure=_log_refresh_failure,
        # The same Copilot BYOK client as the CLI, so the same field fix applies.
        dropped_chat_completions_fields=("reasoning_effort",),
        port=port,
        user_agent=bridge_user_agent(ug_version()),
    )
    # The service manager stops this with SIGTERM, which exits without cleanup.
    server.serve_forever()
