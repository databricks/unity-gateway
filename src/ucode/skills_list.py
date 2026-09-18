"""`ug skills list`: show every configured skill and how it reaches each coding agent.

Merges the two ways a skill is configured -- downloaded to disk (``skills_state``) and a
schema scoped into the skills MCP connection (``mcp``) -- into one table.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.table import Table

from ucode.databricks import get_databricks_token
from ucode.mcp import configured_skill_workspace_and_mcp_locations
from ucode.skills_api import list_schema_skills
from ucode.skills_state import list_downloaded
from ucode.state import load_state
from ucode.ui import console, print_heading, print_note, print_warning

_LOCAL = "local"
_MCP = "mcp"
_BOTH = "local,mcp"
_ALL_AGENTS = "all"


@dataclass(frozen=True)
class ConfiguredSkill:
    name: str
    location: str
    via: str
    agents: str


def _mcp_skill_agents(
    state: dict,
) -> tuple[dict[str, frozenset[str]], list[tuple[str, frozenset[str]]]]:
    """The skills MCP connection's reach, as ``(agents_by_fqn, unlisted_schemas)``.

    ``agents_by_fqn`` maps each reachable skill's fully-qualified name to the agents scoped to
    it. Each scoped schema is listed once against its workspace; a schema whose listing fails
    becomes a ``(location, agents)`` entry in ``unlisted_schemas`` so it still surfaces.
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


def _configured_skills(state: dict) -> list[ConfiguredSkill]:
    """Every configured skill as rows sorted by name then location.

    Keyed on fully-qualified name: a skill downloaded to disk (``local``) is visible to every
    agent, a skill in a schema scoped into the skills MCP connection (``mcp``) is visible to that
    schema's agents, and a skill configured both ways shows ``local,mcp``.
    """
    downloaded_fqns = {record["fqn"] for record in list_downloaded() if record.get("fqn")}
    mcp_agents, unlisted_schemas = _mcp_skill_agents(state)

    rows: list[ConfiguredSkill] = []
    for fqn in downloaded_fqns | mcp_agents.keys():
        location, securable = fqn.rsplit(".", 1)
        in_download, in_mcp = fqn in downloaded_fqns, fqn in mcp_agents
        if in_download and in_mcp:
            rows.append(ConfiguredSkill(securable, location, _BOTH, _ALL_AGENTS))
        elif in_download:
            rows.append(ConfiguredSkill(securable, location, _LOCAL, _ALL_AGENTS))
        else:
            rows.append(
                ConfiguredSkill(securable, location, _MCP, ",".join(sorted(mcp_agents[fqn])))
            )
    for location, clients in unlisted_schemas:
        rows.append(
            ConfiguredSkill(f"(skills in {location})", location, _MCP, ",".join(sorted(clients)))
        )

    rows.sort(key=lambda skill: (skill.name, skill.location))
    return rows


def list_configured_skills_command() -> int:
    """`ug skills list`: print every configured skill and how it reaches each coding agent."""
    rows = _configured_skills(load_state())
    if not rows:
        print_note("No skills configured. Use `ug skills add` to configure skills.")
        return 0

    print_heading("Configured Skills")
    table = Table(box=None, pad_edge=False, header_style="bold")
    for header in ("NAME", "LOCATION", "VIA", "AGENTS"):
        table.add_column(header)
    for row in rows:
        table.add_row(row.name, row.location, row.via, row.agents)
    console.print(table)

    console.print()
    print_note("Use `ug skills add` / `ug skills remove` to change the skills ug configures.")
    return 0
