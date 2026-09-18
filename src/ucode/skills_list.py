"""`ug skills list`: show every configured skill and how it reaches each coding agent.

Merges the two ways a skill is configured -- downloaded to disk (``skills_state``) and a
schema scoped into the skills MCP connection (``mcp``) -- into one table.
"""

from __future__ import annotations

from dataclasses import dataclass

from ucode.databricks import get_databricks_token
from ucode.mcp import configured_skill_workspace_and_mcp_locations
from ucode.skills_api import list_schema_skills
from ucode.skills_state import list_downloaded
from ucode.state import load_state
from ucode.ui import console, print_heading, print_note, print_warning, render_box_table

_DOWNLOADED = "downloaded"
_SKILL_MCP = "skill mcp"
_BOTH = "both (discouraged)"
_ALL_AGENTS = "all"


@dataclass(frozen=True)
class ConfiguredSkill:
    name: str
    location: str
    method: str
    agents: str


def _mcp_skill_agents(
    state: dict,
) -> tuple[dict[str, frozenset[str]], list[tuple[str, frozenset[str]]]]:
    """The skills MCP connection's reach, as ``(agents_by_fqn, unlisted_schemas)``.

    ``agents_by_fqn`` maps each reachable skill's fully-qualified name to the agents scoped to
    it. Each scoped schema is listed once against its workspace; a schema whose listing fails
    becomes an ``(location, agents)`` entry in ``unlisted_schemas`` so it still surfaces.
    """
    configured = configured_skill_workspace_and_mcp_locations(state)
    if configured is None:
        return {}, []
    workspace, locations_by_client = configured
    agents_by_schema: dict[str, set[str]] = {}
    for client, locations in locations_by_client.items():
        for location in locations:
            agents_by_schema.setdefault(location, set()).add(client)
    if not agents_by_schema:
        return {}, []

    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    token = get_databricks_token(workspace, profile)
    agents_by_fqn: dict[str, frozenset[str]] = {}
    unlisted_schemas: list[tuple[str, frozenset[str]]] = []
    for location, clients in agents_by_schema.items():
        catalog, schema = location.split(".")
        refs, reason = list_schema_skills(workspace, token, catalog, schema)
        if reason:
            print_warning(f"Could not list skills in `{location}`: {reason}.")
            unlisted_schemas.append((location, frozenset(clients)))
            continue
        for ref in refs:
            agents_by_fqn[ref.fqn] = frozenset(clients)
    return agents_by_fqn, unlisted_schemas


def list_configured_skills_command() -> int:
    """Print every configured skill and how it reaches each coding agent.

    Keys on fully-qualified name: a skill downloaded to disk is visible to every agent, a skill
    in a schema scoped into the skills MCP connection is visible to that schema's agents, and a
    skill configured both ways is flagged as discouraged.
    """
    downloaded_fqns = {record["fqn"] for record in list_downloaded() if record.get("fqn")}
    mcp_agents, unlisted_schemas = _mcp_skill_agents(load_state())

    rows: list[ConfiguredSkill] = []
    for fqn in downloaded_fqns | mcp_agents.keys():
        location, securable = fqn.rsplit(".", 1)
        in_download, in_mcp = fqn in downloaded_fqns, fqn in mcp_agents
        if in_download and in_mcp:
            rows.append(ConfiguredSkill(securable, location, _BOTH, _ALL_AGENTS))
        elif in_download:
            rows.append(ConfiguredSkill(securable, location, _DOWNLOADED, _ALL_AGENTS))
        else:
            agents = ",".join(sorted(mcp_agents[fqn]))
            rows.append(ConfiguredSkill(securable, location, _SKILL_MCP, agents))
    for location, clients in unlisted_schemas:
        agents = ",".join(sorted(clients))
        rows.append(ConfiguredSkill(f"(skills in {location})", location, _SKILL_MCP, agents))

    if not rows:
        print_note("No skills configured. Use `ug skills add` to configure skills.")
        return 0

    rows.sort(key=lambda skill: (skill.name, skill.location))
    print_heading("Configured Skills")
    console.print(
        render_box_table(
            ["NAME", "UC LOCATION", "CONFIGURATION METHOD", "AGENTS"],
            [[row.name, row.location, row.method, row.agents] for row in rows],
        )
    )
    return 0
