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
    ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR,
)
from ucode.smart_routing.config import resolve_environment
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator"
DISABLED_CONTEXT = (
    "Smart Router Orchestrator is off for this session. This supersedes earlier "
    "Smart Router Orchestrator instructions. Continue in the root unless the user explicitly "
    "requests subagents; use native tools and the current Smart Router setting for those requests. "
    "Collect results from children already running."
)


def feature_enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    source = resolve_environment(env, agent=agent)
    return source.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"


def ownership_decision_enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    """Whether Claude owns orchestration through the pinned local decision bundle."""
    if agent != AGENT_CLAUDE:
        return False
    source = resolve_environment(env, agent=agent)
    return source.get(ENABLE_SMART_ROUTER_OWNERSHIP_DECISION_ENV_VAR) == "1"


def enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    from ucode.smart_routing.v2 import smart_routing_enabled

    source = os.environ if env is None else env
    if not feature_enabled(source, agent=agent):
        return False
    if source.get("ISAAC_LAUNCH_MODE", "").strip().lower() == "omni":
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


def add_claude_agents(plugin_dir: Path, *, env: Mapping[str, str] | None = None) -> None:
    """Load roles alongside the router's exact-model agents, only for this launch."""
    if ownership_decision_enabled(env, agent=AGENT_CLAUDE):
        from ucode.config_io import write_text_file
        from ucode.smart_routing import ownership

        roles = ownership.load_policy().get("roles")
        expected_roles = {"explorer", "researcher", "worker", "tester", "reviewer"}
        if not isinstance(roles, dict) or set(roles) != expected_roles:
            raise ownership.OwnershipSetupError(
                "ownership policy roles do not match the Claude bundle"
            )
        for role, body in roles.items():
            if not isinstance(body, str) or not body:
                raise ownership.OwnershipSetupError(f"ownership policy role {role!r} is invalid")
            write_text_file(plugin_dir / "agents" / f"{role}.md", body)
    elif feature_enabled(env, agent=AGENT_CLAUDE):
        shutil.copytree(skill_directory() / "agents", plugin_dir / "agents", dirs_exist_ok=True)


def sync_hooks(doc: dict, *, agent: str, env: Mapping[str, str] | None = None) -> None:
    groups = {}
    if ownership_decision_enabled(env, agent=agent):
        argv = [sys.executable, "-m", HOOK_MODULE, "--agent", agent]
        hook = {
            "type": "command",
            "command": shlex.join(argv),
            "timeout": 5,
        }
        groups = {"SessionStart": [{"matcher": "compact", "hooks": [hook]}]}
    elif feature_enabled(env, agent=agent):
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


def _compaction_context(payload: Mapping[str, object]) -> str | None:
    from ucode.smart_routing import ownership

    policy = ownership.load_policy()
    session_id = payload.get("session_id")
    state = (
        ownership.read_state(session_id)
        if isinstance(session_id, str) and session_id.strip()
        else None
    )
    current = state.get("current") if state is not None else None
    if (
        isinstance(session_id, str)
        and isinstance(current, dict)
        and current.get("dispatch_status") == "undecided"
    ):
        from ucode.smart_routing.ownership_transport import pending_instructions

        return pending_instructions(session_id, current["request_id"])
    if current is None:
        return None
    marker = session_env_path().parent / claude_answer_delivery.CAPABILITY_FILE
    delivery_available = marker.is_file()
    context = ownership.instructions_for_decision(
        current, policy, answer_delivery=delivery_available
    )
    if state is not None:
        summary = [f"Restored native ownership state (generation {state['generation']}):"]
        if state["owners"]:
            summary.append(
                "owners: "
                + "; ".join(
                    f"agent_id={owner['agent_id']}, scope={owner['scope']}, state={owner['state']}"
                    for owner in state["owners"]
                )
            )
        context = f"{context}\n\n" + "\n".join(summary)
    return context


def hook_output(payload: object, *, agent: str) -> dict | None:
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    event = payload.get("hook_event_name")
    if ownership_decision_enabled(agent=agent):
        # The native prompt.submit hook is the sole initial workflow injector.
        if event == "UserPromptSubmit":
            return None
        if event != "SessionStart" or payload.get("source") != "compact":
            return None
        context = _compaction_context(payload) if enabled(agent=agent) else DISABLED_CONTEXT
        if context is None:
            return None
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}

    if event != "UserPromptSubmit" and not (
        event == "SessionStart" and payload.get("source") == "compact"
    ):
        return None
    context = DISABLED_CONTEXT
    if enabled(agent=agent):
        directory = skill_directory()
        try:
            workflow = skills.skill_entrypoint(directory, agent).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        context = (
            "Smart Router Orchestrator is on for this session. Apply the workflow below.\n"
            f"Skill directory: {directory}\n\n{workflow}"
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
