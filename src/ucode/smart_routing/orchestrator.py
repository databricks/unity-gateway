"""Enforce Smart Router Orchestrator availability in UG launches."""

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

import tomlkit

from ucode import config_io, skills
from ucode.codex_config import codex_config_args
from ucode.constants import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR,
    SMART_ROUTER_CONFIG_VERSION_ENV_VAR,
)
from ucode.smart_routing.config import resolve_environment
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    SESSION_PYTHON_ENV_VAR,
    effective_environment,
    session_env_path,
    start_session,
)

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


def enabled(env: Mapping[str, str] | None = None, *, agent: str) -> bool:
    source = os.environ if env is None else env
    if not feature_enabled(source, agent=agent):
        return False
    try:
        # An installed skill alone does not activate the workflow.
        if not session_env_path(source).is_file():
            return False
    except (RuntimeError, OSError):
        return False
    return feature_enabled(effective_environment(source, agent=agent), agent=agent)


def sync_launch_config(doc: dict, *, agent: str, session_path: Path | None = None) -> None:
    """Set native skill availability and activation for this launch only."""
    active = feature_enabled(agent=agent)
    home = config_io.APP_DIR.parent
    if active:
        # Do not silently launch without a requested workflow.
        skills.install_skill(skills.SMART_ROUTER_ORCHESTRATOR_SKILL, agent, home)
    sync_hooks(doc, agent=agent)
    if agent == AGENT_CODEX:
        codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
        entries = doc.get("skills.config", doc.get("skills", {}).get("config"))
        if entries is None:
            for name in ("ucode.config.toml", "config.toml"):
                entries = (
                    config_io.read_toml_safe(codex_home / name).get("skills", {}).get("config")
                )
                if entries is not None:
                    break
        paths = {
            root / "skills" / skills.SMART_ROUTER_ORCHESTRATOR_SKILL / "SKILL.md"
            for root in (codex_home, home / ".codex", home / ".agents")
        }
        for parent in (Path.cwd(), *Path.cwd().parents):
            path = (
                parent / ".agents" / "skills" / skills.SMART_ROUTER_ORCHESTRATOR_SKILL / "SKILL.md"
            )
            if path.is_file():
                paths.add(path)
        other = []
        for entry in entries or []:
            path = Path(entry["path"]).expanduser()
            if path.parent.name == skills.SMART_ROUTER_ORCHESTRATOR_SKILL:
                paths.add(path)
            else:
                other.append(entry)
        doc["skills.config"] = other + [
            {"path": str(path), "enabled": active} for path in sorted(paths)
        ]
        if active:
            doc["features.hooks"] = True
    else:
        doc.setdefault("skillOverrides", {})[skills.SMART_ROUTER_ORCHESTRATOR_SKILL] = (
            "on" if active else "off"
        )
    launch_env = {
        ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR: "1" if active else "0",
        SMART_ROUTER_CONFIG_VERSION_ENV_VAR: "",
    }
    if active:
        launch_env[SESSION_ENV_VAR] = str(
            session_path if session_path is not None else start_session()
        )
        launch_env[SESSION_PYTHON_ENV_VAR] = sys.executable
    if agent == AGENT_CODEX:
        for key, value in launch_env.items():
            doc[f"shell_environment_policy.set.{key}"] = value
    else:
        doc.setdefault("env", {}).update(launch_env)


def codex_launch_args(
    tool_args: list[str], doc: dict, *, session_path: Path | None = None
) -> list[str]:
    """Apply the flag after caller config overrides, preserving their other skills/hooks."""
    insertion = 0
    before_prompt = tool_args[: tool_args.index("--")] if "--" in tool_args else tool_args
    for index, arg in enumerate(before_prompt):
        if arg in ("-c", "--config") and index + 1 < len(before_prompt):
            value = before_prompt[index + 1]
            insertion = index + 2
        elif arg.startswith("--config="):
            value = arg.removeprefix("--config=")
            insertion = index + 1
        elif arg.startswith("-c") and arg != "-c":
            value = arg[2:].removeprefix("=")
            insertion = index + 1
        else:
            continue
        if value.partition("=")[0].partition(".")[0].strip('"') in {"skills", "hooks"}:
            config_io.deep_merge_dict(doc, tomlkit.parse(value))
            doc.pop("skills.config", None)
    sync_launch_config(doc, agent=AGENT_CODEX, session_path=session_path)
    return [*tool_args[:insertion], *codex_config_args(doc), *tool_args[insertion:]]


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
            workflow = skills.skill_entrypoint(directory, agent).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        context = (
            "Smart Router Orchestrator is on for this session. Apply the workflow below.\n"
            f"Skill directory: {directory}\n\n{workflow}"
        )
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
