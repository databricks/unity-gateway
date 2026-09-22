"""Codex hook configuration for smart subagent routing."""

from __future__ import annotations

import copy
import json
import shlex
import subprocess
from pathlib import Path

from ucode.config_io import is_dry_run, write_json_file
from ucode.databricks import build_auth_token_argv
from ucode.smart_routing import hooks

ROUTING_HOOK_COMMAND_MARKER = "codex-router-hook"
# Keep the original owner value stable so already-trusted hook definitions stay trusted.
SMART_ROUTING_HOOK_OWNER = "ug-codex-app-subagent-only"


def reconcile_smart_routing_hooks_file(path: Path, state: dict, *, enabled: bool) -> None:
    """Install or remove UG's user-level Codex hooks for the current routing mode."""
    if not enabled and not path.exists():
        return
    try:
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("top-level value is not an object")
        else:
            doc = {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot update Codex smart-routing hooks because {path} is not valid JSON: {exc}"
        ) from exc

    original = copy.deepcopy(doc)
    sync_smart_routing_hooks(
        doc,
        state,
        enabled=enabled,
        owner=SMART_ROUTING_HOOK_OWNER,
    )
    if doc == original:
        return
    if doc or is_dry_run():
        write_json_file(path, doc)
    else:
        path.unlink(missing_ok=True)


def routing_models(state: dict) -> list[str]:
    """Return the configured model services compatible with Codex routing."""
    models: list[str] = []
    for key in ("codex_models", "oss_models"):
        values = state.get(key)
        if isinstance(values, list):
            models.extend(value for value in values if isinstance(value, str) and value)
    return list(dict.fromkeys(models))


def sync_smart_routing_hooks(
    doc: dict,
    state: dict,
    *,
    enabled: bool,
    owner: str | None = None,
) -> None:
    """Synchronize ucode-managed routing hooks in a Codex config document."""
    marker = owner or ROUTING_HOOK_COMMAND_MARKER
    groups = _routing_hook_groups(state, owner=owner) if enabled else {}
    hooks.sync_managed_hooks(doc, marker, groups)


def remove_smart_routing_hooks(doc: dict) -> bool:
    """Remove only ucode-managed smart-routing hooks."""
    return hooks.remove_managed_hooks(doc, ROUTING_HOOK_COMMAND_MARKER)


def _routing_hook_groups(state: dict, *, owner: str | None = None) -> dict[str, list[dict]]:
    session_argv = _routing_hook_argv(state, "session-start", owner=owner)
    subagent_argv = _routing_hook_argv(state, "record-subagent", owner=owner)
    return {
        "PreToolUse": [_pre_tool_use_hook_group(state, owner=owner)],
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


def _pre_tool_use_hook_group(
    state: dict,
    *,
    available_models: list[str] | None = None,
    owner: str | None = None,
) -> dict:
    route_argv = _routing_hook_argv(
        state,
        "route-subagent",
        available_models=available_models,
        owner=owner,
    )
    return {
        "matcher": "Agent|.*spawn_agent$",
        "hooks": [_routing_command_hook(route_argv, status="Routing subagent model")],
    }


def _routing_hook_argv(
    state: dict,
    event: str,
    *,
    available_models: list[str] | None = None,
    owner: str | None = None,
) -> list[str]:
    workspace = str(state.get("workspace") or "")
    argv = [
        build_auth_token_argv(workspace, state.get("profile"), use_pat=bool(state.get("use_pat")))[
            0
        ],
        ROUTING_HOOK_COMMAND_MARKER,
        event,
    ]
    if owner:
        argv += ["--hook-owner", owner]
    if event != "route-subagent":
        return argv
    argv += ["--host", workspace]
    profile = state.get("profile")
    if isinstance(profile, str) and profile:
        argv += ["--profile", profile]
    if state.get("use_pat"):
        argv.append("--use-pat")
    models = available_models if available_models is not None else routing_models(state)
    for model in models:
        if isinstance(model, str) and model:
            argv += ["--model", model]
    return argv


def _routing_command_hook(argv: list[str], *, status: str | None = None) -> dict:
    hook = {
        "type": "command",
        "command": shlex.join(argv),
        "command_windows": subprocess.list2cmdline(argv),
        "timeout": 35,
    }
    if status:
        hook["statusMessage"] = status
    return hook
