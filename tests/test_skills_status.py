"""Tests for skills_status.py — the ug skill status data model, JSON, and rendering."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ucode import skills_state, skills_status
from ucode.skills_state import SkillInstall

WORKSPACE = "https://ws.databricks.com"


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _install(
    base: Path,
    fqn: str,
    bundle_name: str,
    *,
    scope: str = "project",
    workspace: str = WORKSPACE,
    make_dirs: bool = True,
) -> SkillInstall:
    dirs = tuple(
        str(base / family / bundle_name) for family in (".claude/skills", ".agents/skills")
    )
    if make_dirs:
        for directory in dirs:
            Path(directory).mkdir(parents=True, exist_ok=True)
            (Path(directory) / "SKILL.md").write_text("bundle")
    return SkillInstall(
        fqn=fqn,
        bundle_name=bundle_name,
        workspace=workspace,
        scope=scope,
        base=str(base),
        dirs=dirs,
        metastore_id="metastore-1",
        skill_id="skill-1",
        uc_update_time="2026-01-01T00:00:00Z",
    )


def _skills_entry(locations, clients, *, by_client=None):
    entry = {
        "name": "databricks-skill-registry",
        "kind": "skills",
        "skill_locations": locations,
        "url": f"{WORKSPACE}/ai-gateway/skills/",
        "auth": "proxy",
        "clients": clients,
    }
    if by_client is not None:
        entry["skill_locations_by_client"] = by_client
    return entry


def _state(mcp_servers=None):
    return {"workspace": WORKSPACE, "mcp_servers": mcp_servers or []}


class TestCollectDownloads:
    def test_sorts_by_fqn_so_schemas_group(self, tmp_path):
        skills_state.record_downloads(
            [
                _install(tmp_path, "system.ai.sql_optimizer", "sql_optimizer"),
                _install(tmp_path, "acme.tools.deploy_helper", "deploy_helper"),
                _install(tmp_path, "system.ai.python_debugging", "python_debugging"),
            ]
        )

        status = skills_status.collect(_state())

        assert [skill.fqn for skill in status.downloaded] == [
            "acme.tools.deploy_helper",
            "system.ai.python_debugging",
            "system.ai.sql_optimizer",
        ]
        assert [skill.schema for skill in status.downloaded] == [
            "acme.tools",
            "system.ai",
            "system.ai",
        ]

    def test_prunes_records_with_a_missing_dir_but_keeps_files(self, tmp_path):
        skills_state.record_downloads(
            [
                _install(tmp_path, "system.ai.keep", "keep"),
                _install(tmp_path, "system.ai.gone", "gone"),
            ]
        )
        surviving = tmp_path / ".agents/skills/gone"
        shutil.rmtree(tmp_path / ".claude/skills/gone")

        status = skills_status.collect(_state())

        assert [skill.fqn for skill in status.downloaded] == ["system.ai.keep"]
        assert status.pruned == ["system.ai.gone"]
        assert [record["fqn"] for record in skills_state.list_downloaded()] == ["system.ai.keep"]
        assert surviving.exists()

    def test_path_filters_downloads_but_still_prunes_globally(self, tmp_path):
        project = tmp_path / "proj"
        home = tmp_path / "home"
        skills_state.record_downloads(
            [
                _install(project, "acme.tools.deploy_helper", "deploy_helper"),
                _install(home, "system.ai.python_debugging", "python_debugging", scope="user"),
                _install(home, "system.ai.gone", "gone", scope="user", make_dirs=False),
            ]
        )

        status = skills_status.collect(_state(), base=str(project))

        assert [skill.fqn for skill in status.downloaded] == ["acme.tools.deploy_helper"]
        assert status.pruned == ["system.ai.gone"]

    def test_surfaces_origin_workspace_when_it_differs(self, tmp_path, capsys):
        skills_state.record_downloads(
            [
                _install(
                    tmp_path, "system.ai.debug", "debug", workspace="https://other.databricks.com"
                )
            ]
        )

        status = skills_status.collect(_state())
        skills_status.render(status)

        assert status.downloaded[0].workspace == "https://other.databricks.com"
        assert "https://other.databricks.com" in _strip_ansi(capsys.readouterr().out)


class TestMcpScope:
    def test_unconfigured_when_no_skills_entry(self):
        status = skills_status.collect(_state())
        assert status.mcp.configured is False
        assert skills_status.to_json(status)["mcp"] == {"configured": False}

    def test_shared_scope_collapses_in_render_but_stays_per_agent_in_json(self, capsys):
        state = _state([_skills_entry(["system.ai", "acme.tools"], ["claude", "codex"])])

        status = skills_status.collect(state)
        skills_status.render(status)
        out = _strip_ansi(capsys.readouterr().out)

        assert "Configured: Claude Code, Codex" in out
        assert "Schemas: system.ai, acme.tools" in out
        assert skills_status.to_json(status)["mcp"]["by_agent"] == {
            "claude": ["system.ai", "acme.tools"],
            "codex": ["system.ai", "acme.tools"],
        }

    def test_divergent_scopes_render_per_agent(self, capsys):
        state = _state(
            [
                _skills_entry(
                    ["main.default", "claude.only"],
                    ["claude", "codex"],
                    by_client={
                        "claude": ["main.default", "claude.only"],
                        "codex": ["main.default"],
                    },
                )
            ]
        )

        status = skills_status.collect(state)
        skills_status.render(status)
        out = _strip_ansi(capsys.readouterr().out)

        assert "Claude Code: main.default, claude.only" in out
        assert "Codex: main.default" in out


class TestJson:
    def test_skill_fields_map_to_removal_flags(self, tmp_path):
        skills_state.record_downloads(
            [_install(tmp_path, "system.ai.python_debugging", "python_debugging", scope="user")]
        )
        state = _state([_skills_entry(["system.ai"], ["claude"])])

        payload = skills_status.to_json(skills_status.collect(state))

        assert payload["downloaded"]["count"] == 1
        assert payload["downloaded"]["pruned"] == []
        skill = payload["downloaded"]["skills"][0]
        assert skill["fqn"] == "system.ai.python_debugging"
        assert skill["schema"] == "system.ai"
        assert skill["skill_dir"] == "python_debugging"
        assert skill["scope"] == "user"
        assert skill["base"] == str(tmp_path)
        assert payload["mcp"] == {
            "configured": True,
            "server": "databricks-skill-registry",
            "workspace": WORKSPACE,
            "by_agent": {"claude": ["system.ai"]},
        }

    def test_empty_state_is_well_typed(self):
        payload = skills_status.to_json(skills_status.collect(_state()))
        assert payload["downloaded"] == {"count": 0, "pruned": [], "skills": []}
        assert payload["mcp"] == {"configured": False}


class TestRenderEmpty:
    def test_reports_no_downloads_and_unconfigured_mcp(self, capsys):
        skills_status.render(skills_status.collect(_state()))
        out = _strip_ansi(capsys.readouterr().out)
        assert "None downloaded." in out
        assert "Not configured." in out
