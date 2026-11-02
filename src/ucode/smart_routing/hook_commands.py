"""Smart-routing hook commands that Claude Code and Codex run at session start and per subagent.

`ucode.entrypoint` runs them through `run` without loading the full CLI; `ucode.cli` registers the
same functions, so parsing and help are identical. Session and subagent records only write local
files, so the Databricks client, launch module, and saved config scope load only for routing events.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated

import typer

from ucode.constants import AGENT_CLAUDE, AGENT_CODEX
from ucode.smart_routing.claude_hooks import (
    FIRST_PROMPT_SOCKET_ENV,
    ROUTE_FIRST_PROMPT_EVENT,
    ROUTE_SUBAGENT_MOD_EVENT,
)
from ucode.smart_routing.config import smart_routing_enabled
from ucode.smart_routing.session_env import effective_environment

# Keep in sync with `ucode.entrypoint._AGENT_HELPERS`, which can't import this module.
COMMANDS = ("claude-router-hook", "codex-router-hook")


def _oauth_token_is_fresh(token: str, buffer_seconds: float = 120) -> bool:
    import base64
    import binascii
    import time

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        expires_at = float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return False
    return time.time() < expires_at - buffer_seconds


def _router_hook_token(host: str, profile: str | None, use_pat: bool) -> str | None:
    from ucode import custom_oauth
    from ucode.databricks import ensure_pat_bearer
    from ucode.state import load_full_state

    # The launch-time OAUTH_TOKEN is never refreshed, so a long session must not reuse it expired.
    if use_pat and not ensure_pat_bearer(profile):
        return None
    token = os.environ.get("DATABRICKS_BEARER", "").strip()
    if token:
        return token
    token = os.environ.get("OAUTH_TOKEN", "").strip()
    if _oauth_token_is_fresh(token):
        return token
    # Refresh through the same client the launch used, including CLI-backed custom OAuth.
    state = {**load_full_state().get("workspaces", {}).get(host, {}), "profile": profile}
    try:
        return custom_oauth.launch_token(state, host, force_refresh=True)
    except RuntimeError:
        return None


def _apply_config_scope() -> None:
    # Routing reads managed agent settings, which the saved scope can exclude; the CLI's root
    # callback applies it before every subcommand.
    from ucode.managed_files import set_config_scope
    from ucode.state import load_config_scope

    set_config_scope(load_config_scope())


def codex_router_hook_cmd(
    event: str,
    host: Annotated[str | None, typer.Option("--host")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    use_pat: Annotated[bool, typer.Option("--use-pat")] = False,
    model: Annotated[list[str] | None, typer.Option("--model")] = None,
    workspace_id: Annotated[str | None, typer.Option("--workspace-id", hidden=True)] = None,
) -> None:
    """Run a Codex smart-routing lifecycle hook."""
    if host and workspace_id:
        from ucode.workspace import pin

        pin(host, workspace_id)

    if not smart_routing_enabled(
        effective_environment(agent=AGENT_CODEX), default=False, agent=AGENT_CODEX
    ):
        return

    from ucode.smart_routing.codex_routing import (
        record_session_start,
        record_subagent_start,
        route_pre_tool_use,
    )

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if event == "session-start":
        record_session_start(payload)
        return
    if event == "record-subagent":
        record = record_subagent_start(payload)
        matched = record.get("matches_router_decision")
        if matched is True:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing verified. "
                        f"Subagent is using {record.get('model')}."
                    }
                )
            )
        elif matched is False:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing mismatch: router requested "
                        f"{record.get('requested_model')}, but Codex started "
                        f"{record.get('model')}."
                    }
                )
            )
        # When matched is None the harness didn't report the subagent model —
        # the PreToolUse hook already injected the routed model, so emit nothing.
        return
    if event != "route-subagent" or not host:
        return
    _apply_config_scope()
    token = _router_hook_token(host, profile, use_pat)
    if not token:
        return
    output = route_pre_tool_use(
        payload,
        workspace=host,
        token=token,
        available_models=model or [],
        audit_decision=True,
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))


def claude_router_hook_cmd(
    event: str,
    host: Annotated[str | None, typer.Option("--host")] = None,
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    use_pat: Annotated[bool, typer.Option("--use-pat")] = False,
    model: Annotated[list[str] | None, typer.Option("--model")] = None,
    socket_path: Annotated[str | None, typer.Option("--socket")] = None,
    workspace_id: Annotated[str | None, typer.Option("--workspace-id", hidden=True)] = None,
) -> None:
    """Run a Claude Code smart-routing lifecycle hook."""
    if host and workspace_id:
        from ucode.workspace import pin

        pin(host, workspace_id)

    if not smart_routing_enabled(
        effective_environment(agent=AGENT_CLAUDE), default=False, agent=AGENT_CLAUDE
    ):
        return

    from ucode.smart_routing.claude_routing import (
        record_session_start,
        record_subagent_start,
    )

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if event == ROUTE_FIRST_PROMPT_EVENT:
        if not socket_path:
            socket_path = os.environ.get(FIRST_PROMPT_SOCKET_ENV)
        if not socket_path:
            return
        from pathlib import Path

        from ucode.smart_routing import v2 as smart_routing_v2
        from ucode.smart_routing.claude_pty import (
            first_prompt_hook_output,
            request_first_prompt_route,
        )

        response = request_first_prompt_route(
            Path(socket_path),
            payload,
            timeout=smart_routing_v2.CLAUDE_ROUTE_SELECTION_TIMEOUT_S + 5.0,
        )
        output = first_prompt_hook_output(response)
        if output is not None:
            sys.stdout.write(json.dumps(output))
        return
    if event == "session-start":
        record_session_start(payload)
        return
    if event == "record-subagent":
        record = record_subagent_start(payload)
        matched = record.get("matches_router_decision")
        if matched is True:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing verified. "
                        f"Subagent is using {record.get('model')}."
                    }
                )
            )
        elif matched is False:
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": "Smart Routing mismatch: router requested "
                        f"{record.get('requested_model')}, but Claude Code started "
                        f"{record.get('model')}."
                    }
                )
            )
        # When matched is None the harness didn't report the subagent model —
        # routing already injected the selected model, so emit nothing.
        return
    if event not in {"route-subagent", ROUTE_SUBAGENT_MOD_EVENT} or not host:
        return
    _apply_config_scope()
    token = _router_hook_token(host, profile, use_pat)
    if not token:
        return
    from ucode.smart_routing import v2 as smart_routing_v2

    output = smart_routing_v2.route_claude_subagent(
        payload,
        workspace=host,
        token=token,
        available_models=model or [],
        audit_decision=True,
        # Hooks persisted by earlier UG versions still call route-subagent and expect a
        # PreToolUse response; the mod's route-subagent-mod event takes the plain details.
        hook_output=event == "route-subagent",
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))


def register(app: typer.Typer) -> None:
    """Add the hook commands to the full CLI or to `run`'s hook-only app."""
    app.command("codex-router-hook", hidden=True)(codex_router_hook_cmd)
    app.command("claude-router-hook", hidden=True)(claude_router_hook_cmd)


def run(argv: list[str]) -> None:
    """Run `ug <hook> ...` with only the hook commands registered."""
    app = typer.Typer(add_completion=False)
    register(app)
    app(args=argv, prog_name="ug")
