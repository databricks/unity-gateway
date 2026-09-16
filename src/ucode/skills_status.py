"""Read-side view of configured skills, backing ``ug skill status`` and the ``ug status`` summary.

Skills reach a coding agent two independent ways: downloaded bundles (the ``~/.ucode/skills.json``
manifest plus their on-disk dirs) and schemas attached to the skills MCP connection (``state.json``).
:func:`collect` gathers both into one :class:`SkillStatus`, which :func:`render` prints for humans and
:func:`to_json` emits for agents, so every surface reads the same data. Collecting also reconciles the
manifest: a record whose on-disk directory is gone is pruned (see :func:`skills_state.forget`), so the
listing only ever shows skills that are actually installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ucode import skills_state
from ucode.mcp import (
    MCP_CLIENTS,
    SKILLS_MCP_SERVER_NAME,
    _skill_locations_by_client,
    _skills_entry,
    _skills_workspace,
    agents_share_one_scope,
)
from ucode.ui import console, heading, print_heading, print_kv, print_note

_DOWNLOAD_ADD = (
    "Add:    ug skill add    --skills <fqn>  or  --location <catalog.schema>  [--path <base>]"
)
_DOWNLOAD_REMOVE = (
    "Remove: ug skill remove --skills <fqn>  or  --location <catalog.schema>  [--path <base>]"
)
_MCP_ADD = "Add:    ug skill add    --mcp --location <catalog.schema>  [--agents <agent>]"
_MCP_REMOVE = "Remove: ug skill remove --mcp --location <catalog.schema>  [--agents <agent>]"


@dataclass(frozen=True)
class DownloadedSkill:
    fqn: str
    schema: str
    skill_dir: str
    base: str
    scope: str
    dirs: tuple[str, ...]
    workspace: str | None
    workspace_id: str | None
    downloaded_at: str | None
    uc_update_time: str | None


@dataclass(frozen=True)
class McpScope:
    configured: bool
    server: str | None = None
    workspace: str | None = None
    by_agent: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillStatus:
    workspace: str | None
    downloaded: list[DownloadedSkill]
    pruned: list[str]
    mcp: McpScope


def collect(state: dict, base: str | None = None) -> SkillStatus:
    """Assemble skill status from the manifest and MCP state, pruning stale download records.

    ``base`` limits the downloaded listing to one download base, mirroring ``--path``; the MCP scope
    is global and unaffected.
    """
    downloaded, pruned = _reconciled_downloads(base)
    return SkillStatus(
        workspace=state.get("workspace"),
        downloaded=downloaded,
        pruned=pruned,
        mcp=_mcp_scope(state),
    )


def _reconciled_downloads(base: str | None) -> tuple[list[DownloadedSkill], list[str]]:
    stale: list[dict] = []
    live: list[dict] = []
    for record in skills_state.list_downloaded():
        (stale if skills_state.record_dirs_missing(record) else live).append(record)
    skills_state.forget(stale)
    pruned = sorted(str(record.get("fqn", "")) for record in stale)
    skills = sorted(
        (_downloaded_skill(record) for record in live if _under_base(record, base)),
        key=lambda skill: (skill.fqn, skill.base),
    )
    return skills, pruned


def _under_base(record: dict, base: str | None) -> bool:
    return base is None or os.path.normpath(record.get("base", "")) == os.path.normpath(base)


def _downloaded_skill(record: dict) -> DownloadedSkill:
    fqn = str(record.get("fqn", ""))
    return DownloadedSkill(
        fqn=fqn,
        schema=fqn.rsplit(".", 1)[0] if "." in fqn else fqn,
        skill_dir=str(record.get("bundle_name", "")),
        base=str(record.get("base", "")),
        scope=str(record.get("scope", "")),
        dirs=tuple(record.get("dirs") or []),
        workspace=record.get("workspace"),
        workspace_id=record.get("workspace_id"),
        downloaded_at=record.get("downloaded_at"),
        uc_update_time=record.get("uc_update_time"),
    )


def _mcp_scope(state: dict) -> McpScope:
    entry = _skills_entry(list(state.get("mcp_servers") or []))
    if entry is None:
        return McpScope(configured=False)
    return McpScope(
        configured=True,
        server=str(entry.get("name") or SKILLS_MCP_SERVER_NAME),
        workspace=_skills_workspace(entry) or state.get("workspace"),
        by_agent=_skill_locations_by_client(entry),
    )


def to_json(status: SkillStatus) -> dict:
    return {
        "workspace": status.workspace,
        "downloaded": {
            "count": len(status.downloaded),
            "pruned": status.pruned,
            "skills": [_skill_json(skill) for skill in status.downloaded],
        },
        "mcp": _mcp_json(status.mcp),
    }


def _skill_json(skill: DownloadedSkill) -> dict:
    return {
        "fqn": skill.fqn,
        "schema": skill.schema,
        "skill_dir": skill.skill_dir,
        "base": skill.base,
        "scope": skill.scope,
        "dirs": list(skill.dirs),
        "workspace": skill.workspace,
        "workspace_id": skill.workspace_id,
        "downloaded_at": skill.downloaded_at,
        "uc_update_time": skill.uc_update_time,
    }


def _mcp_json(mcp: McpScope) -> dict:
    if not mcp.configured:
        return {"configured": False}
    return {
        "configured": True,
        "server": mcp.server,
        "workspace": mcp.workspace,
        "by_agent": mcp.by_agent,
    }


def render(status: SkillStatus) -> None:
    console.print(heading("ug skill status"))
    _render_downloaded(status)
    _render_mcp(status.mcp)


def _render_downloaded(status: SkillStatus) -> None:
    print_heading(f"Downloaded skills ({len(status.downloaded)})")
    for base, scope, skills in _group_by_base(status.downloaded):
        console.print(f"  Base {_base_label(base)} ({scope})")
        for skill in skills:
            foreign = skill.workspace and skill.workspace != status.workspace
            origin = f"  [dim]· from[/dim] {skill.workspace}" if foreign else ""
            console.print(
                f"    {skill.fqn}  [dim]· skill dir[/dim] {skill.skill_dir}"
                f"  [dim]· downloaded[/dim] {skill.downloaded_at or 'unknown'}{origin}"
            )
    if not status.downloaded:
        console.print("  None downloaded.")
    if status.pruned:
        print_note(
            f"Pruned {len(status.pruned)} stale record(s) whose skill dir was deleted: "
            + ", ".join(status.pruned)
        )
    print_note(_DOWNLOAD_ADD)
    if status.downloaded:
        print_note(_DOWNLOAD_REMOVE)


def _render_mcp(mcp: McpScope) -> None:
    print_heading("Skill MCP scope")
    if not mcp.configured:
        console.print("  Not configured.")
        print_note(_MCP_ADD)
        return
    print_kv("Server", mcp.server or SKILLS_MCP_SERVER_NAME)
    if agents_share_one_scope(mcp.by_agent):
        locations = next(iter(mcp.by_agent.values()), [])
        agents = ", ".join(str(MCP_CLIENTS[client]["display"]) for client in mcp.by_agent)
        print_kv("Configured", agents or "none")
        print_kv("Schemas", ", ".join(locations) if locations else "none — utility tools only")
    else:
        for client, locations in mcp.by_agent.items():
            print_kv(
                str(MCP_CLIENTS[client]["display"]),
                ", ".join(locations) if locations else "none — utility tools only",
            )
    print_note(_MCP_ADD)
    print_note(_MCP_REMOVE)


def _group_by_base(skills: list[DownloadedSkill]) -> list[tuple[str, str, list[DownloadedSkill]]]:
    groups: dict[str, list[DownloadedSkill]] = {}
    for skill in skills:
        groups.setdefault(skill.base, []).append(skill)
    ordered = sorted(groups.items(), key=lambda item: (item[1][0].scope != "user", item[0]))
    return [(base, members[0].scope, members) for base, members in ordered]


def _base_label(base: str) -> str:
    return "~" if base == str(Path.home()) else base
