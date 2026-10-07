"""Activate the bundled orchestrator only in an enabled smart-routing session."""

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
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator"
DISABLED_CONTEXT = (
    "UG automatic orchestration is off because smart routing is off for this session. "
    "This supersedes any earlier model-orchestrator workflow: do not start new automatic "
    "delegation or fall back to orchestrator role models. Explicit user requests for subagents "
    "still use native tools and normal harness model selection, without the orchestrator "
    "or its routing check; keep routing off. Otherwise continue the task in the root. "
    "Collect results from children already running."
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
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
    return smart_routing_enabled(effective_environment(source))


def require_enabled() -> None:
    if not enabled():
        raise ValueError(DISABLED_CONTEXT)


def skill_directory() -> Path:
    return skills._skills_source() / skills.ORCHESTRATOR_SKILL


def add_claude_agents(plugin_dir: Path) -> None:
    """Load roles alongside the router's exact-model agents, only for this launch."""
    shutil.copytree(skill_directory() / "agents", plugin_dir / "agents", dirs_exist_ok=True)


def sync_hooks(doc: dict, *, agent: str) -> None:
    argv = [sys.executable, "-m", HOOK_MODULE]
    hook = {
        "type": "command",
        "command": shlex.join(argv),
        "timeout": 5,
    }
    if agent == "codex":
        hook["command_windows"] = subprocess.list2cmdline(argv)
    sync_managed_hooks(
        doc,
        HOOK_MODULE,
        {
            "UserPromptSubmit": [{"hooks": [hook]}],
            "SessionStart": [{"matcher": "compact", "hooks": [hook]}],
        },
    )


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
            "Apply the UG model-orchestrator workflow to this task. "
            "Smart routing and automatic orchestration share the same session controls.\n"
            f"Skill directory: {directory}\n\n{workflow}"
        )
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit successfully only in an enabled smart-routing session.",
    )
    if parser.parse_args(argv).check:
        try:
            require_enabled()
        except ValueError as exc:
            parser.exit(1, f"{exc}\n")
        return

    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, ValueError):
        return
    output = hook_output(payload)
    if output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
