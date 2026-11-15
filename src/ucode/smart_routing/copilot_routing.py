"""Databricks Unity Gateway routing helpers for GitHub Copilot CLI subagents.

Copilot delegates through its ``task`` tool. A ``preToolUse`` hook sends the subagent's prompt to the
shared router and returns ``modifiedArgs`` with a ``model`` set to the routed registry model
(``<provider>/<model id>``), which Copilot honors over the agent definition and the parent model.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ucode.config_io import APP_DIR
from ucode.constants import AGENT_COPILOT
from ucode.databricks import build_auth_token_argv
from ucode.smart_routing import orchestrator, routing
from ucode.smart_routing.routing import RoutingDecision
from ucode.workspace import workspace_id_args

ROUTING_HOOK_COMMAND_MARKER = "copilot-router-hook"
TASK_TOOL_NAME = "task"
DECISIONS_PATH = APP_DIR / "copilot-smart-routing-decisions.jsonl"
SUBAGENT_NOTICE_CONFIG = routing.SubagentNoticeConfig(name_field="name", prompt_field="prompt")
# The production recipe accepts only Claude and Codex harness labels; Copilot is offered as Claude.
ROUTE_HARNESS = "claude"
HOOK_TIMEOUT_S = 35
# The router rejects a request offering more distinct models.
MAX_ROUTE_OPTIONS = 20


def is_task_tool(tool_name: Any) -> bool:
    """Return whether a hook payload names Copilot's subagent-delegation tool."""
    return isinstance(tool_name, str) and tool_name.strip().lower() == TASK_TOOL_NAME


def model_id(ref: str) -> str:
    """The Databricks model id of a registry ref such as ``dbx-anthropic/system.ai.claude-x``."""
    return ref.partition("/")[2] or ref


def request_routing_decision(
    workspace: str,
    token: str,
    task: str,
    refs: Iterable[str],
    *,
    timeout: float = routing.REQUEST_TIMEOUT_S,
) -> tuple[RoutingDecision | None, str | None]:
    """Ask the router for one of the registry models Copilot can run."""
    route_options = routing.route_options_for((model_id(ref) for ref in refs), ROUTE_HARNESS)
    if not route_options:
        return None, "no Copilot models are available"
    return routing.select_route(
        workspace,
        token,
        task,
        route_options,
        router_name=routing.configured_router_name(),
        timeout=timeout,
    )


def route_pre_tool_use(
    payload: dict[str, Any],
    *,
    workspace: str,
    token: str,
    refs: list[str],
    timeout: float = routing.REQUEST_TIMEOUT_S,
    decision_fn: Callable[[str], tuple[RoutingDecision | None, str | None]] | None = None,
) -> dict[str, Any] | None:
    """Route one ``task`` call and return Copilot's ``preToolUse`` output rewriting its model."""
    tool_args = payload.get("toolArgs")
    adapted = {
        "tool_name": payload.get("toolName"),
        "tool_input": tool_args,
        "session_id": payload.get("sessionId"),
        "tool_use_id": payload.get("toolCallId"),
    }
    ref_by_id = {model_id(ref): ref for ref in refs}
    route = routing.resolve_spawn_route(
        adapted,
        is_spawn_agent=is_task_tool,
        decision_fn=decision_fn
        or (lambda task: request_routing_decision(workspace, token, task, refs, timeout=timeout)),
        default_task_label="Copilot subagent task",
        model_id_mapper=lambda model: ref_by_id.get(model, model),
    )
    if route is None:
        return None
    routing.write_decision_record(
        DECISIONS_PATH, adapted, route.task, route.decision, route.routed_model
    )
    return {
        "permissionDecision": "allow",
        "permissionDecisionReason": SUBAGENT_NOTICE_CONFIG.message(
            route.decision, route.routed_model, route.tool_input
        ),
        "modifiedArgs": {**route.tool_input, "model": route.routed_model},
    }


def hook_argv(state: dict, refs: Iterable[str]) -> list[str]:
    """The ``ug`` command Copilot runs before each tool call."""
    workspace = str(state.get("workspace") or "")
    argv = [
        build_auth_token_argv(workspace, state.get("profile"), use_pat=bool(state.get("use_pat")))[
            0
        ],
        ROUTING_HOOK_COMMAND_MARKER,
        "route-subagent",
        "--host",
        workspace,
        *workspace_id_args(workspace),
    ]
    profile = state.get("profile")
    if isinstance(profile, str) and profile:
        argv += ["--profile", profile]
    if state.get("use_pat"):
        argv.append("--use-pat")
    # ponytail: first 20 in catalog order (newest Claude, then GPT); select per family if open models matter.
    for ref in list(dict.fromkeys(refs))[:MAX_ROUTE_OPTIONS]:
        argv += ["--model", ref]
    return argv


def write_plugin(directory: Path, argv: list[str]) -> Path:
    """Write the launch-scoped Copilot plugin whose hook routes ``task`` calls.

    Loaded with ``--plugin-dir``, it needs no change to the user's Copilot configuration.
    """
    (directory / ".claude-plugin").mkdir(parents=True)
    (directory / "hooks").mkdir()
    (directory / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "ug-smart-routing",
                "version": "1.0.0",
                "description": "Unity Gateway smart routing for Copilot subagents",
                "hooks": "./hooks/hooks.json",
            }
        ),
        encoding="utf-8",
    )
    hook = {
        "type": "command",
        # Spawning ug on every tool call would slow the session for no routing benefit.
        "matcher": TASK_TOOL_NAME,
        "bash": shlex.join(argv),
        "powershell": subprocess.list2cmdline(argv),
        "timeoutSec": HOOK_TIMEOUT_S,
    }
    hooks = {"preToolUse": [hook]}
    if orchestrator.feature_enabled(agent=AGENT_COPILOT):
        command = [sys.executable, "-m", orchestrator.HOOK_MODULE, "--agent", AGENT_COPILOT]
        hooks["sessionStart"] = [
            {
                "type": "command",
                "bash": shlex.join(command),
                "powershell": subprocess.list2cmdline(command),
                "timeoutSec": 5,
            }
        ]
    (directory / "hooks" / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": hooks}), encoding="utf-8"
    )
    return directory
