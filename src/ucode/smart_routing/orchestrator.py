"""Gate the bundled orchestrator on an enabled smart-routing session."""

from __future__ import annotations

import os
from collections.abc import Mapping

from ucode.smart_routing.session_env import effective_environment, session_env_path

DISABLED_CONTEXT = (
    "UG automatic orchestration is off because smart routing is off for this session. "
    "This supersedes any earlier model-orchestrator workflow: do not start new automatic "
    "delegation or fall back to default role models. Continue the task in the root; "
    "collect results from children already running."
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
