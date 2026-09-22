#!/usr/bin/env python3
"""Replay the parallel Claude agent-registration failure captured in the TUI.

The failing session launched four top-level Agent calls concurrently. Its agent
registry contained built-in and plugin agents but none of the route agents passed
through ``--agents``. This script sends the same four concurrent hook payloads and
checks whether each selected route agent exists in that captured registry shape.

Before the fix:
    uv run python scripts/repro_stale_claude_subagent.py --registration cli --expect broken

After the fix:
    uv run python scripts/repro_stale_claude_subagent.py --registration plugin --expect fixed
"""

from __future__ import annotations

import argparse
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ucode.smart_routing import routing, v2

AVAILABLE_MODEL = "system.ai.glm-5-3"
AVAILABLE_AGENTS = {
    "Explore",
    "Plan",
    "general-purpose",
    "model-orchestrator:explorer",
    "model-orchestrator:worker",
}
TASKS = (
    "Audit anti-patterns and type safety",
    "Audit architecture and complexity",
    "Audit test quality",
    "Audit hooks, data-fetching, and correctness",
)


def _plugin_agents(plugin_dir: Path) -> set[str]:
    agents = set()
    for agent_path in (plugin_dir / "agents").glob("*.md"):
        name_line = next(
            line for line in agent_path.read_text().splitlines() if line.startswith("name: ")
        )
        name = json.loads(name_line.removeprefix("name: "))
        agents.add(f"{v2.CLAUDE_ROUTING_PLUGIN_NAME}:{name}")
    return agents


def _route(task: str) -> str:
    output = v2.route_claude_pre_tool_use(
        {
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "Explore",
                "prompt": task,
                "model": "sonnet",
            },
        },
        workspace="https://example.databricks.com",
        token="test-token",
        available_models=[AVAILABLE_MODEL],
    )
    return output["hookSpecificOutput"]["updatedInput"]["subagent_type"]


def reproduce(registration: str) -> list[str]:
    registered_agents = set(AVAILABLE_AGENTS)
    with tempfile.TemporaryDirectory() as temp_dir:
        if registration == "plugin":
            plugin_dir = Path(temp_dir) / "routing-plugin"
            v2._write_routed_claude_plugin(plugin_dir, [AVAILABLE_MODEL])
            registered_agents.update(_plugin_agents(plugin_dir))

        decision = routing.RoutingDecision(model=AVAILABLE_MODEL, raw_model="glm-5-3")
        with (
            patch(
                "ucode.smart_routing.v2.routing.select_route",
                return_value=(decision, None),
            ),
            ThreadPoolExecutor(max_workers=len(TASKS)) as executor,
        ):
            selected_agents = list(executor.map(_route, TASKS))

    if registration == "cli":
        prefix = f"{v2.CLAUDE_ROUTING_PLUGIN_NAME}:"
        selected_agents = [agent.removeprefix(prefix) for agent in selected_agents]
    return [agent for agent in selected_agents if agent not in registered_agents]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registration", choices=("cli", "plugin"), default="plugin")
    parser.add_argument("--expect", choices=("broken", "fixed"), default="fixed")
    args = parser.parse_args()

    missing_agents = reproduce(args.registration)
    if missing_agents:
        print(
            f"Agent type '{missing_agents[0]}' not found. "
            f"Available agents: {', '.join(sorted(AVAILABLE_AGENTS))}"
        )
        print(f"FAILED: {len(missing_agents)} concurrent Agent calls selected missing agents")
    else:
        print(f"PASS: all {len(TASKS)} concurrent Agent calls selected registered plugin agents")

    actual = "broken" if missing_agents else "fixed"
    if actual != args.expect:
        print(f"Expected {args.expect}, observed {actual}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
