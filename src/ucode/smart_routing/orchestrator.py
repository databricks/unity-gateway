"""Activate the bundled orchestrator only in an enabled smart-routing session."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ucode import codex_config, skills
from ucode.config_io import read_json_safe, read_toml_safe
from ucode.smart_routing.hooks import sync_managed_hooks
from ucode.smart_routing.session_env import effective_environment, session_env_path

HOOK_MODULE = "ucode.smart_routing.orchestrator"
DISABLED_CONTEXT = (
    "UG automatic orchestration is off because smart routing is off for this session. "
    "This supersedes any earlier model-orchestrator workflow: do not start new automatic "
    "delegation or fall back to orchestrator role models. Explicit user requests for subagents "
    "still use native tools and normal harness model selection, without the orchestrator "
    "or its model-resolution helper; keep routing off. Otherwise continue the task in the root. "
    "Collect results from children already running."
)


def _legacy_plugin_ids(plugins: object) -> set[str]:
    if not isinstance(plugins, dict):
        return set()
    return {
        name
        for name in plugins
        if isinstance(name, str) and name.partition("@")[0] == "model-orchestrator"
    }


def suppress_legacy_claude_plugin(settings: dict, user_settings_path: Path) -> bool:
    """Suppress the installed predecessor for this launch, including when routing is off."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", user_settings_path.parent)).expanduser()
    installed = read_json_safe(config_dir / "plugins" / "installed_plugins.json")
    user_settings = read_json_safe(config_dir / user_settings_path.name)
    names = (
        _legacy_plugin_ids(installed.get("plugins"))
        | _legacy_plugin_ids(user_settings.get("enabledPlugins"))
        | _legacy_plugin_ids(settings.get("enabledPlugins"))
    )
    if not names:
        return False
    plugins = settings.setdefault("enabledPlugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("Claude settings 'enabledPlugins' must be an object.")
    plugins.update(dict.fromkeys(sorted(names), False))
    return True


def legacy_codex_plugin_config(
    profile_path: Path | None = None, *, cwd: Path | None = None
) -> dict:
    """Return a launch override; Isaac owns and may regenerate the saved plugin entries."""
    names: set[str] = set()
    directory = (cwd or Path.cwd()).resolve()
    paths = codex_config.codex_config_precedence_paths(
        codex_config.codex_managed_config_path(),
        profile_path or codex_config.DEFAULT_CODEX_CONFIG_PATH,
    )
    # Only collect IDs to disable; never promote commands from untrusted project files.
    project_paths = []
    for parent in (directory, *directory.parents):
        project_paths.append(parent / ".codex" / "config.toml")
        if (parent / ".git").exists():
            break
    for path in (*paths, *project_paths):
        names.update(_legacy_plugin_ids(read_toml_safe(path).get("plugins")))
    # Codex merges this table with the saved map. A dotted key cannot safely encode
    # arbitrary marketplace names, and quoted dotted segments are treated literally.
    return {"plugins": {name: {"enabled": False} for name in sorted(names)}} if names else {}


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


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (OSError, UnicodeError, ValueError):
        return
    output = hook_output(payload)
    if output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
