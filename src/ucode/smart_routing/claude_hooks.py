"""Claude Code hook configuration for smart subagent routing.

Written into ``~/.claude/ucode-settings.json`` under Claude Code's hook events.
The native Claude mod routes subagent calls. ``SessionStart`` and
``SubagentStart`` hooks drive the canary/audit trail.
"""

from __future__ import annotations

import shlex

from ucode.databricks import build_auth_token_argv
from ucode.smart_routing import hooks
from ucode.workspace import workspace_id_args

ROUTING_HOOK_COMMAND_MARKER = "claude-router-hook"
ROUTE_FIRST_PROMPT_EVENT = "route-first-prompt"
ROUTE_SUBAGENT_MOD_EVENT = "route-subagent-mod"
FIRST_PROMPT_HOOK_MARKER = f"{ROUTING_HOOK_COMMAND_MARKER} {ROUTE_FIRST_PROMPT_EVENT}"
FIRST_PROMPT_SOCKET_ENV = "UCODE_CLAUDE_V2_SOCKET"


def sync_smart_routing_hooks(doc: dict, state: dict, *, enabled: bool) -> None:
    """Synchronize ucode-managed lifecycle hooks, removing legacy routing hooks."""
    groups = _routing_hook_groups(state) if enabled else {}
    hooks.sync_managed_hooks(doc, ROUTING_HOOK_COMMAND_MARKER, groups)


def remove_smart_routing_hooks(doc: dict) -> bool:
    """Remove only ucode-managed smart-routing hooks."""
    return hooks.remove_managed_hooks(doc, ROUTING_HOOK_COMMAND_MARKER)


def sync_first_prompt_hook(doc: dict, executable: str) -> None:
    """Add the first-prompt hook to a per-launch settings document."""
    groups = {
        "UserPromptSubmit": [
            {
                "hooks": [
                    _routing_command_hook(
                        [executable, ROUTING_HOOK_COMMAND_MARKER, ROUTE_FIRST_PROMPT_EVENT],
                        status="Selecting a model with Smart Routing",
                    )
                ]
            }
        ]
    }
    hooks.sync_managed_hooks(doc, FIRST_PROMPT_HOOK_MARKER, groups)


def _routing_hook_groups(state: dict) -> dict[str, list[dict]]:
    session_argv = routing_hook_argv(state, "session-start")
    subagent_argv = routing_hook_argv(state, "record-subagent")
    return {
        "SessionStart": [
            {
                "matcher": "startup|resume|clear",
                "hooks": [_routing_command_hook(session_argv)],
            }
        ],
        "SubagentStart": [
            {
                "hooks": [_routing_command_hook(subagent_argv)],
            }
        ],
    }


def routing_hook_argv(state: dict, event: str) -> list[str]:
    """Build the shared CLI command for a lifecycle hook or native mod request."""
    workspace = str(state.get("workspace") or "")
    argv = [
        build_auth_token_argv(workspace, state.get("profile"), use_pat=bool(state.get("use_pat")))[
            0
        ],
        ROUTING_HOOK_COMMAND_MARKER,
        event,
    ]
    if event != ROUTE_SUBAGENT_MOD_EVENT:
        return argv
    argv += ["--host", workspace, *workspace_id_args(workspace)]
    profile = state.get("profile")
    if isinstance(profile, str) and profile:
        argv += ["--profile", profile]
    if state.get("use_pat"):
        argv.append("--use-pat")
    # The route-subagent hook resolves the router's chosen arm back to a routable
    # workspace id, so it needs the discovered claude model ids.
    claude_models = state.get("claude_models")
    if isinstance(claude_models, dict):
        for model in claude_models.values():
            if isinstance(model, str) and model:
                argv += ["--model", model]
    return argv


def _routing_command_hook(argv: list[str], *, status: str | None = None) -> dict:
    hook = {
        "type": "command",
        "command": shlex.join(argv),
        "timeout": 35,
    }
    if status:
        hook["statusMessage"] = status
    return hook
