"""Inference-payload metadata shared by smart-routed agent integrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ucode.config_io import atomic_write_json
from ucode.smart_routing.routing import configured_router_name

SMART_ROUTER_RECIPE_FIELD = "smart_router_recipe_name"
UG_ROUTING_STATE_TAG = "ug-routing-state"
CLAUDE_CODE_EXTRA_BODY_ENV_VAR = "CLAUDE_CODE_EXTRA_BODY"


def smart_router_recipe_payload(
    enabled: bool, env: Mapping[str, str] | None = None
) -> dict[str, str | None]:
    """Return the recipe override for an opted-in session's next inference."""
    return {
        SMART_ROUTER_RECIPE_FIELD: configured_router_name(env) if enabled else None,
    }


def smart_router_recipe_marker(enabled: bool, env: Mapping[str, str] | None = None) -> str:
    """Encode the recipe override as an exact developer-message marker."""
    payload = json.dumps(
        smart_router_recipe_payload(enabled, env),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"<{UG_ROUTING_STATE_TAG}>{payload}</{UG_ROUTING_STATE_TAG}>"


def merge_claude_recipe_extra_body(
    settings: dict, enabled: bool, env: Mapping[str, str] | None = None
) -> bool:
    """Merge the recipe override into Claude's extra body without losing other fields.

    Returns ``False`` without mutating ``settings`` when the existing environment or extra-body
    value is malformed.
    """
    settings_env = settings.get("env")
    if settings_env is None:
        settings_env = {}
    if not isinstance(settings_env, dict):
        return False

    raw_extra_body = settings_env.get(CLAUDE_CODE_EXTRA_BODY_ENV_VAR)
    if raw_extra_body is None:
        extra_body = {}
    elif not isinstance(raw_extra_body, str):
        return False
    else:
        try:
            extra_body = json.loads(raw_extra_body)
        except json.JSONDecodeError:
            return False
        if not isinstance(extra_body, dict):
            return False

    updated_body = {
        **extra_body,
        **smart_router_recipe_payload(enabled, env),
    }
    updated_env = {
        **settings_env,
        CLAUDE_CODE_EXTRA_BODY_ENV_VAR: json.dumps(
            updated_body,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    settings["env"] = updated_env
    return True


def update_claude_session_recipe(
    watched_settings_path: Path,
    enabled: bool,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Atomically update an existing, explicitly session-owned watched settings file.

    The caller is responsible for proving that Claude watches this exact per-session file. This
    helper deliberately does not discover user, managed, or arbitrary ``--settings`` paths.
    """
    try:
        settings = json.loads(watched_settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(settings, dict) or not merge_claude_recipe_extra_body(settings, enabled, env):
        return False
    atomic_write_json(watched_settings_path, settings)
    return True
