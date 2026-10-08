"""Claude request-body metadata backed by the current session's routing controls."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ucode import gateway_proxy
from ucode.constants import SMART_ROUTER_RECIPE_FIELD, SMART_ROUTER_RECIPE_LOCAL
from ucode.smart_routing.routing import configured_router_name
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    SESSION_PYTHON_ENV_VAR,
    read_session_environment,
    set_session_environment,
)

CLAUDE_CODE_EXTRA_BODY = "CLAUDE_CODE_EXTRA_BODY"


def merge_claude_recipe_extra_body(raw: str | None, recipe: str) -> str:
    """Own the recipe field while retaining all other caller-supplied body fields."""
    try:
        payload = json.loads(raw) if raw is not None else {}
    except (ValueError, TypeError) as exc:
        raise RuntimeError("CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("CLAUDE_CODE_EXTRA_BODY must contain a valid JSON object.")
    payload[SMART_ROUTER_RECIPE_FIELD] = recipe
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def session_recipe(path: Path) -> str:
    """Read the authoritative session value afresh for each outbound inference."""
    recipe = read_session_environment(path).get(SMART_ROUTER_RECIPE_LOCAL)
    if not recipe:
        raise RuntimeError("Smart Router session recipe is missing.")
    return recipe


@contextmanager
def claude_recipe_session(settings: dict, session_path: Path) -> Iterator[dict]:
    """Keep a session's body metadata current even when Claude caches startup settings.

    Claude inherits the initial EXTRA_BODY in its launch settings. The loopback forwarder reads
    the shared session state for every parent/child inference, replacing only the recipe field.
    """
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise RuntimeError("Claude settings 'env' must be an object for smart routing.")
    upstream_url = env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
    if not isinstance(upstream_url, str) or not upstream_url:
        raise RuntimeError("Smart Router needs ANTHROPIC_BASE_URL; run `ug configure` first.")
    recipe = configured_router_name()
    extra_body = merge_claude_recipe_extra_body(
        env.get(CLAUDE_CODE_EXTRA_BODY, os.environ.get(CLAUDE_CODE_EXTRA_BODY)), recipe
    )
    set_session_environment({SMART_ROUTER_RECIPE_LOCAL: recipe})
    env.update(
        {
            "SMART_ROUTER_NAME": recipe,
            SMART_ROUTER_RECIPE_LOCAL: recipe,
            CLAUDE_CODE_EXTRA_BODY: extra_body,
            SESSION_ENV_VAR: str(session_path),
            SESSION_PYTHON_ENV_VAR: os.environ[SESSION_PYTHON_ENV_VAR],
        }
    )
    server, client = gateway_proxy.start_claude_recipe_proxy(
        upstream_url, lambda: session_recipe(session_path)
    )
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    launch_env = {
        key: env[key]
        for key in (
            "ANTHROPIC_BASE_URL",
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
        server.shutdown()
        server.server_close()
        server_thread.join()
        client.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
