"""Smart Router Orchestrator hook entry point.

The hook runs on every prompt, so a session whose Smart Router is off answers here without
importing the rest of ug. Every other session continues in ``orchestrator.main``.
"""

from __future__ import annotations

import json
import sys

from ucode.constants import ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR
from ucode.smart_routing import session_state

DISABLED_CONTEXT = (
    "Smart Router Orchestrator is off for this session. This supersedes earlier "
    "Smart Router Orchestrator instructions. Continue in the root unless the user explicitly "
    "requests subagents; use native tools and the current Smart Router setting for those requests. "
    "Collect results from children already running."
)


def prompt_event(payload: object) -> str | None:
    """Return the root-session event that carries orchestrator context, if this is one."""
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit" or (
        event == "SessionStart" and payload.get("source") == "compact"
    ):
        return event
    return None


def context_output(event: str, context: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def main() -> None:
    switch = session_state.read_switch()
    if switch is None or switch["enabled"]:
        from ucode.smart_routing import orchestrator

        orchestrator.main()
        return
    try:
        event = prompt_event(json.load(sys.stdin))
    except (OSError, UnicodeError, ValueError):
        return
    # Until the first `on`, prompts stay exactly as they are in a session without Smart Router.
    if (
        event
        and switch["ever_enabled"]
        and switch["when_on"].get(ENABLE_SMART_ROUTER_ORCHESTRATOR_ENV_VAR) == "1"
    ):
        print(json.dumps(context_output(event, DISABLED_CONTEXT)))


if __name__ == "__main__":
    main()
