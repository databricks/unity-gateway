"""`ug skills list`: show every configured skill and how it reaches each coding agent.

Merges the two ways a skill is configured -- downloaded to disk (``skills_state``) and a
schema scoped into the skills MCP connection (``mcp``) -- into one table.
"""

from __future__ import annotations

from dataclasses import dataclass

from ucode.databricks import get_databricks_token
from ucode.mcp import configured_skill_mcp_locations
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


def _downloaded_skills() -> dict[str, tuple[str, str]]:
    """Downloaded skills as ``fqn -> (securable_name, "<catalog>.<schema>")``, one entry per fqn."""
    by_fqn: dict[str, tuple[str, str]] = {}
    for record in list_downloaded():
        fqn = record.get("fqn")
        if fqn:
            location, securable = fqn.rsplit(".", 1)
            by_fqn.setdefault(fqn, (securable, location))
    return by_fqn


def _mcp_skills(state: dict) -> dict[str, tuple[str, str, frozenset[str]]]:
    """Skills reachable through the skills MCP connection, keyed by fully-qualified name.

    Value is ``(securable_name, "<catalog>.<schema>", agents)``. Each scoped schema is listed
    once against its workspace; a schema whose listing fails contributes one placeholder
    entry so it still appears in the output.
    """
    configured = configured_skill_mcp_locations(state)
    if configured is None:
        return {}
    workspace, locations_by_client = configured
    agents_by_schema: dict[str, set[str]] = {}
    for client, locations in locations_by_client.items():
        for location in locations:
            agents_by_schema.setdefault(location, set()).add(client)
    if not agents_by_schema:
        return {}

    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    token = get_databricks_token(workspace, profile)
    by_fqn: dict[str, tuple[str, str, frozenset[str]]] = {}
    for location, clients in agents_by_schema.items():
        agents = frozenset(clients)
        catalog, schema = location.split(".")
        refs, reason = list_schema_skills(workspace, token, catalog, schema)
        if reason:
            print_warning(f"Could not list skills in `{location}`: {reason}.")
            by_fqn[f"{location}.*"] = (f"(skills in {location})", location, agents)
            continue
        for ref in refs:
            by_fqn[ref.fqn] = (ref.securable_name, f"{ref.catalog}.{ref.schema}", agents)
    return by_fqn


def list_configured_skills_command() -> int:
    """Print every configured skill and how it reaches each coding agent.

    Keys on fully-qualified name: a skill downloaded to disk is visible to every agent, a skill
    in a schema scoped into the skills MCP connection is visible to that schema's agents, and a
    skill configured both ways is flagged as discouraged.
    """
    state = load_state()
    downloaded = _downloaded_skills()
    mcp_skills = _mcp_skills(state)

    rows: list[ConfiguredSkill] = []
    for fqn in downloaded.keys() | mcp_skills.keys():
        in_download, in_mcp = fqn in downloaded, fqn in mcp_skills
        name, location = downloaded[fqn] if in_download else mcp_skills[fqn][:2]
        if in_download and in_mcp:
            rows.append(ConfiguredSkill(name, location, _BOTH, _ALL_AGENTS))
        elif in_download:
            rows.append(ConfiguredSkill(name, location, _DOWNLOADED, _ALL_AGENTS))
        else:
            agents = ",".join(sorted(mcp_skills[fqn][2]))
            rows.append(ConfiguredSkill(name, location, _SKILL_MCP, agents))

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
