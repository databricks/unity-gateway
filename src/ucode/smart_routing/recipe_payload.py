"""Claude request-body metadata backed by the current session's routing controls."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ucode.agents.claude_settings import merge_extra_body
from ucode.constants import (
    CLAUDE_CODE_EXTRA_BODY,
    SMART_ROUTER_RECIPE_FIELD,
    SMART_ROUTER_RECIPE_LOCAL,
)
from ucode.smart_routing.routing import configured_router_name
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    SESSION_PYTHON_ENV_VAR,
    set_session_environment,
)


def merge_claude_recipe_extra_body(raw: str | None, recipe: str) -> str:
    """Own the recipe field while retaining all other caller-supplied body fields."""
    return merge_extra_body(raw, {SMART_ROUTER_RECIPE_FIELD: recipe})


@contextmanager
def claude_recipe_session(settings: dict, session_path: Path) -> Iterator[dict]:
    """Initialize native Claude extra-body metadata and this session's desired routing state.

    Updating the session file does not mutate a running Claude process's environment or its
    cached startup settings. Native propagation after a toggle still requires a reload mechanism.
    """
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise RuntimeError("Claude settings 'env' must be an object for smart routing.")
    recipe = configured_router_name()
    extra_body = merge_claude_recipe_extra_body(
        env.get(CLAUDE_CODE_EXTRA_BODY, os.environ.get(CLAUDE_CODE_EXTRA_BODY)), recipe
    )
    set_session_environment(
        {SMART_ROUTER_RECIPE_LOCAL: recipe, CLAUDE_CODE_EXTRA_BODY: extra_body}, path=session_path
    )
    env.update(
        {
            "SMART_ROUTER_NAME": recipe,
            SMART_ROUTER_RECIPE_LOCAL: recipe,
            CLAUDE_CODE_EXTRA_BODY: extra_body,
            SESSION_ENV_VAR: str(session_path),
            SESSION_PYTHON_ENV_VAR: os.environ[SESSION_PYTHON_ENV_VAR],
        }
    )
    launch_env = {
        key: env[key]
        for key in (
            "SMART_ROUTER_NAME",
            CLAUDE_CODE_EXTRA_BODY,
            SMART_ROUTER_RECIPE_LOCAL,
            SESSION_ENV_VAR,
            SESSION_PYTHON_ENV_VAR,
        )
    }
    previous = {key: os.environ.get(key) for key in launch_env}
    try:
        os.environ.update(launch_env)
        yield settings
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
