"""Inject configured Unity Gateway integrations before each Cowork prompt."""

from __future__ import annotations

import argparse
import json
import sys

_SERVICE_LABELS = {
    "atlassian": "Atlassian, Jira, and Confluence",
    "github": "GitHub",
}


def routing_guidance(server_names: list[str]) -> str:
    integrations = []
    for name in dict.fromkeys(server_names):
        service = name.removeprefix("system-ai-").removeprefix("system.ai.")
        label = _SERVICE_LABELS.get(service, service.replace("-", " ").title())
        integrations.append(f"{label} (`unity-gateway:{name}`)")
    available = ", ".join(integrations)
    return (
        f"Unity Gateway integrations available in this Cowork session: {available}. "
        "When a request matches one of these services, use that integration before saying the "
        "connector is unavailable, including requests about the user's own resources."
    )


def hook_output(payload: object, server_names: list[str]) -> dict:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "UserPromptSubmit":
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": routing_guidance(server_names),
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", action="append", default=[])
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        output = hook_output(payload, args.server)
    except (OSError, UnicodeError, ValueError):
        output = {}
    print(json.dumps(output))


if __name__ == "__main__":
    main()
