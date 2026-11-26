"""`ug skills list`: show every configured skill and how it reaches each coding agent.

Merges the two ways a skill is configured -- downloaded to disk (``skills_state``) and a
schema scoped into the skills MCP connection (``mcp``) -- into one table.

`ug skills list --available` instead lists the skills the workspace offers in Unity Catalog,
whether or not they are configured, so a developer can find one to add without the picker.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rich.table import Table

from ucode.databricks import get_databricks_token
from ucode.mcp import configured_skill_workspace_and_mcp_locations
from ucode.skills_api import (
    _SKILLS_WALK_DEADLINE_SECONDS,
    _SKILLS_WALK_TIMEOUT_REASON,
    SkillRef,
    list_all_skills,
    list_schema_skills,
)
from ucode.skills_state import list_downloaded
from ucode.state import load_state
from ucode.ui import console, print_heading, print_note, print_warning, spinner

_LOCAL = "local"
_MCP = "mcp"
_BOTH = "local,mcp"
_ALL_AGENTS = "all"
_NOT_CONFIGURED = "-"
_NO_SKILLS_FOUND = "no skills found"


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
    """Every configured skill as rows sorted by location then name.

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

    rows.sort(key=lambda skill: (skill.location, skill.name))
    return rows


def configured_skill_counts_by_agent(state: dict, agents: Iterable[str]) -> dict[str, int]:
    """Count of distinct skills each of ``agents`` can reach, keyed by agent.

    Combines the locally downloaded skills (which every agent can use) with the skills reachable
    through the schemas scoped to that agent in the skills MCP connection. Best effort: lists each
    scoped schema the way ``ug skills list`` does, and any schema that cannot be read (including an
    auth error that stops the read entirely) is skipped rather than failing.
    """
    downloaded = frozenset(record["fqn"] for record in list_downloaded() if record.get("fqn"))
    try:
        agents_by_fqn, _ = _mcp_skill_agents(state)
    except Exception:
        agents_by_fqn = {}
    mcp_by_agent: dict[str, set[str]] = {}
    for fqn, scoped in agents_by_fqn.items():
        for agent in scoped:
            mcp_by_agent.setdefault(agent, set()).add(fqn)
    return {agent: len(downloaded | mcp_by_agent.get(agent, set())) for agent in agents}


def list_configured_skills_command() -> int:
    """`ug skills list`: print every configured skill and how it reaches each coding agent."""
    rows = _configured_skills(load_state())
    if not rows:
        print_note("No skills configured. Use `ug skills add` to configure skills.")
        return 0

    print_heading("Configured Skills")
    console.print()
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2))
    for header in ("NAME", "LOCATION", "VIA", "AGENTS"):
        table.add_column(header)
    for row in rows:
        table.add_row(row.name, row.location, row.via, row.agents)
    console.print(table)

    console.print()
    print_note("Use `ug skills add` / `ug skills remove` to change the skills ug configures.")
    return 0


@dataclass(frozen=True)
class AvailableSkill:
    name: str
    location: str
    configured: str
    description: str


def _configured_via(
    fqn: str, location: str, downloaded: frozenset[str], scoped: frozenset[str]
) -> str:
    """How an available skill is already configured: ``local``, ``mcp``, both, or ``-``."""
    in_download, in_mcp = fqn in downloaded, location in scoped
    if in_download and in_mcp:
        return _BOTH
    if in_download:
        return _LOCAL
    if in_mcp:
        return _MCP
    return _NOT_CONFIGURED


def _available_skills(state: dict, refs: list[SkillRef]) -> list[AvailableSkill]:
    """``refs`` as rows sorted by location then name, each marked with how it is configured.

    Uses only local state for the ``CONFIGURED`` column: a downloaded record, or the skill's
    schema in the skills MCP connection's scope for any agent. No extra workspace calls.
    """
    downloaded = frozenset(record["fqn"] for record in list_downloaded() if record.get("fqn"))
    configured = configured_skill_workspace_and_mcp_locations(state)
    scoped = (
        frozenset(location for locations in configured[1].values() for location in locations)
        if configured is not None
        else frozenset()
    )
    rows = [
        AvailableSkill(
            name=ref.securable_name,
            location=f"{ref.catalog}.{ref.schema}",
            configured=_configured_via(ref.fqn, f"{ref.catalog}.{ref.schema}", downloaded, scoped),
            description=" ".join((ref.description or "").split()),
        )
        for ref in {ref.fqn: ref for ref in refs}.values()
    ]
    rows.sort(key=lambda skill: (skill.location, skill.name))
    return rows


def _list_available_refs(
    workspace: str, token: str, locations: list[str]
) -> tuple[list[SkillRef], str | None]:
    """The finalized skills in ``locations``, or in every readable schema when it is empty.

    A schema in ``locations`` that cannot be listed is warned about and skipped, so the others
    still show. Without ``locations`` this is the workspace-wide walk the ``ug skills add``
    picker uses, which returns partial results with a timeout reason past its deadline.
    """
    if not locations:
        return list_all_skills(workspace, token)
    refs: list[SkillRef] = []
    for location in locations:
        catalog, schema = location.split(".")
        found, reason = list_schema_skills(workspace, token, catalog, schema)
        if reason:
            print_warning(f"Could not list skills in `{location}`: {reason}.")
            continue
        refs.extend(found)
    return refs, None


def list_available_skills_command(locations: list[str]) -> int:
    """`ug skills list --available`: print the skills the workspace offers in Unity Catalog.

    Lists ``locations`` (``<catalog>.<schema>``), or every schema the user can read when empty.
    Read-only: it configures nothing and writes no state.
    """
    state = load_state()
    workspace = state.get("workspace")
    if not workspace:
        raise RuntimeError("Workspace is not configured. Run `ug configure` first.")
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    token = get_databricks_token(workspace, profile)

    scope = ", ".join(f"`{location}`" for location in locations) or "every schema you can read"
    with spinner(f"Listing skills in {scope}..."):
        refs, reason = _list_available_refs(workspace, token, locations)
    if reason == _SKILLS_WALK_TIMEOUT_REASON:
        print_warning(
            f"Timed out after {int(_SKILLS_WALK_DEADLINE_SECONDS)}s; showing the {len(refs)} "
            "skills found so far. Use `--location <catalog.schema>` to list a schema directly."
        )
    elif reason not in (None, _NO_SKILLS_FOUND):
        raise RuntimeError(f"Could not list skills in {workspace}: {reason}.")

    rows = _available_skills(state, refs)
    if not rows:
        print_note(f"No skills found in {scope}.")
        return 0

    print_heading("Available Skills")
    console.print()
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2))
    for header in ("NAME", "LOCATION", "CONFIGURED", "DESCRIPTION"):
        table.add_column(header)
    for row in rows:
        table.add_row(row.name, row.location, row.configured, row.description)
    console.print(table)

    console.print()
    print_note(
        "Add a skill with `ug skills add --names <catalog.schema.skill>`, or a whole schema "
        "with `ug skills add --location <catalog.schema>` (add `--via mcp` to load it live)."
    )
    return 0
