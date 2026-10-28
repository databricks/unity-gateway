"""Activate Smart Router Orchestrator in opted-in smart-routing sessions."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ucode import claude_answer_delivery, skills
from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
)
from ucode.smart_routing.config import resolve_environment
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator"
CLAUDE_AGENT_DESCRIPTION = """Launch a subagent to own a complete task under the active Smart Router Orchestrator workflow.

When orchestration is on, delegate substantive investigation, implementation, debugging,
research, and verification before gathering source evidence. A connected task needs one
owner even when there is no independent parallel work. Handle simple answers and bounded
lookups directly. Follow explicit user ownership choices and the latest activation context;
when orchestration is off, do not start automatic delegation. A delegated worker executes
its assignment directly without delegating again.

Use the available agent types and native input schema, following the workflow's role and
model selection. Give the owner the goal, context, constraints, and acceptance criteria.
Do not duplicate assigned work. Use the existing agent's ID with the native continuation
tools for follow-ups.

An Agent call starts a new worker; calling it again does not wait for an existing one.
When a tool reports a background launch, completion arrives automatically as a
notification in a later turn. This is an incoming user-role message from the host,
never text you write yourself. Do not predict or fabricate results or completion
notifications. If no independent work remains,
end the current turn and wait for that notification. A brief waiting status is enough;
ending this turn lets the host deliver the result without ending the assignment.
Do not launch another worker to wait for it, poll for results, or read its output
file while it runs. Review the received result before answering the user."""
DISABLED_CONTEXT = (
    "Smart Router Orchestrator is off for this session. This supersedes earlier "
    "Smart Router Orchestrator instructions. Continue in the root unless the user explicitly "
    "requests subagents; use native tools and the current Smart Router setting for those requests. "
    "Collect results from children already running."
)


def feature_enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    source = resolve_environment(env, agent=agent)
    return source.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"


def enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    from ucode.smart_routing.v2 import smart_routing_enabled

    source = os.environ if env is None else env
    if not feature_enabled(source, agent=agent):
        return False
    try:
        # The marker is created only after UG selects a supported routing launch.
        if not session_env_path(source).is_file():
            return False
    except (RuntimeError, OSError):
        return False
    return smart_routing_enabled(
        effective_environment(source, agent=agent), default=False, agent=agent
    )


def skill_directory() -> Path:
    return skills._skills_source() / skills.SMART_ROUTER_ORCHESTRATOR_SKILL


def add_claude_agents(plugin_dir: Path) -> None:
    """Load roles alongside the router's exact-model agents, only for this launch."""
    if feature_enabled(agent=AGENT_CLAUDE):
        shutil.copytree(skill_directory() / "agents", plugin_dir / "agents", dirs_exist_ok=True)


def sync_hooks(doc: dict, *, agent: str) -> None:
    groups = {}
    if feature_enabled(agent=agent):
        argv = [sys.executable, "-m", HOOK_MODULE, "--agent", agent]
        hook = {
            "type": "command",
            "command": shlex.join(argv),
            "timeout": 5,
        }
        if agent == AGENT_CODEX:
            hook["command_windows"] = subprocess.list2cmdline(argv)
        groups = {
            "UserPromptSubmit": [{"hooks": [hook]}],
            "SessionStart": [{"matcher": "compact", "hooks": [hook]}],
        }
    sync_managed_hooks(doc, HOOK_MODULE, groups)


def hook_output(payload: object, *, agent: str) -> dict | None:
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    event = payload.get("hook_event_name")
    if event != "UserPromptSubmit" and not (
        event == "SessionStart" and payload.get("source") == "compact"
    ):
        return None
    context = DISABLED_CONTEXT
    if enabled(agent=agent):
        directory = skill_directory()
        try:
            entrypoint = skills.skill_entrypoint(directory, agent)
            workflow = entrypoint.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        guidance = f"{CLAUDE_AGENT_DESCRIPTION}\n\n" if agent == AGENT_CLAUDE else ""
        context = (
            "Smart Router Orchestrator is on for this session. Apply the workflow below.\n"
            f"Skill directory: {directory}\n"
            "If the workflow is truncated and not already in context, read "
            f"{entrypoint.name} in that directory before proceeding.\n\n"
            f"{guidance}{workflow}"
        )
        if agent == AGENT_CLAUDE:
            marker = session_env_path().parent / claude_answer_delivery.CAPABILITY_FILE
            delivery = (
                claude_answer_delivery.AVAILABLE_CONTEXT
                if marker.is_file()
                else claude_answer_delivery.UNAVAILABLE_CONTEXT
            )
            context = f"{delivery}\n\n{context}"
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=(AGENT_CLAUDE, AGENT_CODEX), required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, ValueError):
        return
    output = hook_output(payload, agent=args.agent)
    if output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
