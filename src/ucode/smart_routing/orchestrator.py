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

from ucode import skills
from ucode.constants import ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator"
DISABLED_CONTEXT = (
    "Smart Router Orchestrator is off for this session. This supersedes earlier "
    "Smart Router Orchestrator instructions. Continue in the root unless the user explicitly "
    "requests subagents; use native tools and the current Smart Router setting for those requests. "
    "Collect results from children already running."
)


def feature_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"


def enabled(env: Mapping[str, str] | None = None) -> bool:
    from ucode.smart_routing.v2 import smart_routing_enabled

    source = os.environ if env is None else env
    if not feature_enabled(source):
        return False
    if source.get("ISAAC_LAUNCH_MODE", "").strip().lower() == "omni":
        return False
    try:
        # The marker is created only after UG selects a supported routing launch.
        if not session_env_path(source).is_file():
            return False
    except (RuntimeError, OSError):
        return False
    return smart_routing_enabled(effective_environment(source))


def skill_directory() -> Path:
    return skills._skills_source() / skills.SMART_ROUTER_ORCHESTRATOR_SKILL


def add_claude_agents(plugin_dir: Path) -> None:
    """Load roles alongside the router's exact-model agents, only for this launch."""
    if feature_enabled():
        shutil.copytree(skill_directory() / "agents", plugin_dir / "agents", dirs_exist_ok=True)


def sync_hooks(doc: dict, *, agent: str) -> None:
    groups = {}
    if feature_enabled():
        argv = [sys.executable, "-m", HOOK_MODULE]
        hook = {
            "type": "command",
            "command": shlex.join(argv),
            "timeout": 5,
        }
        if agent == "codex":
            hook["command_windows"] = subprocess.list2cmdline(argv)
        groups = {
            "UserPromptSubmit": [{"hooks": [hook]}],
            "SessionStart": [{"matcher": "compact", "hooks": [hook]}],
        }
    sync_managed_hooks(doc, HOOK_MODULE, groups)


def hook_output(payload: object) -> dict | None:
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    event = payload.get("hook_event_name")
    if event != "UserPromptSubmit" and not (
        event == "SessionStart" and payload.get("source") == "compact"
    ):
        return None
    context = DISABLED_CONTEXT
    if enabled():
        directory = skill_directory()
        try:
            workflow = (directory / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        context = (
            "Smart Router Orchestrator is on for this session. Apply the workflow below.\n"
            f"Skill directory: {directory}\n\n{workflow}"
        )
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, ValueError):
        return
    output = hook_output(payload)
    if output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
