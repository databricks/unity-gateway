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
from ucode.smart_routing.orchestrator_hook import DISABLED_CONTEXT, context_output, prompt_event
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator_hook"
# Also matches hooks that earlier releases pointed at this module.
HOOK_MARKER = "ucode.smart_routing.orchestrator"


def feature_enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    source = resolve_environment(env, agent=agent)
    return source.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"


def enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    from ucode.smart_routing.v2 import smart_routing_enabled

    source = os.environ if env is None else env
    if source.get("ISAAC_LAUNCH_MODE", "").strip().lower() == "omni":
        return False
    try:
        # The marker is created only after UG selects a supported routing launch.
        if not session_env_path(source).is_file():
            return False
    except (RuntimeError, OSError):
        return False
    effective = effective_environment(source, agent=agent)
    return feature_enabled(effective, agent=agent) and smart_routing_enabled(
        effective, default=False, agent=agent
    )


def skill_directory() -> Path:
    return skills._skills_source() / skills.SMART_ROUTER_ORCHESTRATOR_SKILL


def add_claude_agents(plugin_dir: Path) -> None:
    """Load roles alongside the router's exact-model agents, only for this launch."""
    shutil.copytree(skill_directory() / "agents", plugin_dir / "agents", dirs_exist_ok=True)


def sync_hooks(doc: dict, *, agent: str, enabled: bool) -> None:
    groups = {}
    if enabled:
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
    sync_managed_hooks(doc, HOOK_MARKER, groups)


def hook_output(payload: object, *, agent: str) -> dict | None:
    event = prompt_event(payload)
    if event is None:
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
    return context_output(event, context)


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
