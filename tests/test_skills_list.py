"""Tests for skills_list.py — the `ug skills list` command that merges downloaded
and MCP-scoped skills into one table."""

from __future__ import annotations

import pytest

import ucode.skills_list as sl
from ucode.skills_api import SkillRef

WS = "https://example.databricks.com"


def ref(securable_name: str, *, catalog: str = "main", schema: str = "default") -> SkillRef:
    # Bundle name deliberately differs from the securable: the NAME column shows the securable.
    return SkillRef(
        catalog=catalog,
        schema=schema,
        securable_name=securable_name,
        bundle_name=f"{securable_name}-bundle",
    )


def as_tuples(rows):
    return [(row.name, row.location, row.via, row.agents) for row in rows]


class TestConfiguredSkills:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setattr(sl, "get_databricks_token", lambda *a, **k: "token")

    def _downloaded(self, monkeypatch, fqns):
        records = [{"fqn": fqn, "bundle_name": f"{fqn.rsplit('.', 1)[-1]}-bundle"} for fqn in fqns]
        monkeypatch.setattr(sl, "list_downloaded", lambda: records)

    def _mcp(self, monkeypatch, locations_by_client, by_schema):
        monkeypatch.setattr(
            sl,
            "configured_skill_workspace_and_mcp_locations",
            lambda _state: (WS, locations_by_client) if locations_by_client else None,
        )
        monkeypatch.setattr(
            sl, "list_schema_skills", lambda ws, tok, c, s: by_schema.get(f"{c}.{s}", ([], None))
        )

    def test_downloaded_only_is_visible_to_all_agents(self, monkeypatch):
        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(monkeypatch, {}, {})

        assert as_tuples(sl._configured_skills({})) == [("alpha", "main.default", "local", "all")]

    def test_mcp_scope_expands_to_named_skills_for_its_agents(self, monkeypatch):
        self._downloaded(monkeypatch, [])
        self._mcp(
            monkeypatch,
            {"claude": ["main.default"], "codex": ["main.default"]},
            {"main.default": ([ref("alpha")], None)},
        )

        assert as_tuples(sl._configured_skills({})) == [
            ("alpha", "main.default", "mcp", "claude,codex")
        ]

    def test_downloaded_and_scoped_shows_both(self, monkeypatch):
        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(
            monkeypatch, {"claude": ["main.default"]}, {"main.default": ([ref("alpha")], None)}
        )

        assert as_tuples(sl._configured_skills({})) == [
            ("alpha", "main.default", "local,mcp", "all")
        ]

    def test_sorted_by_name_then_location(self, monkeypatch):
        self._downloaded(monkeypatch, ["z.a.beta", "main.default.alpha", "ml.prod.alpha"])
        self._mcp(monkeypatch, {}, {})

        assert [(r.name, r.location) for r in sl._configured_skills({})] == [
            ("alpha", "main.default"),
            ("alpha", "ml.prod"),
            ("beta", "z.a"),
        ]

    def test_repeated_installs_of_one_skill_collapse_to_one_row(self, monkeypatch):
        records = [
            {"fqn": "main.default.alpha", "bundle_name": "alpha", "base": "/home/me"},
            {"fqn": "main.default.alpha", "bundle_name": "alpha", "base": "/work/proj"},
        ]
        monkeypatch.setattr(sl, "list_downloaded", lambda: records)
        self._mcp(monkeypatch, {}, {})

        assert as_tuples(sl._configured_skills({})) == [("alpha", "main.default", "local", "all")]

    def test_failed_schema_listing_keeps_the_scope_with_a_placeholder(self, monkeypatch):
        warnings: list[str] = []
        monkeypatch.setattr(sl, "print_warning", warnings.append)
        self._downloaded(monkeypatch, [])
        self._mcp(
            monkeypatch, {"claude": ["main.default"]}, {"main.default": ([], "HTTP 404 Not Found")}
        )

        assert as_tuples(sl._configured_skills({})) == [
            ("(skills in main.default)", "main.default", "mcp", "claude")
        ]
        assert warnings and "main.default" in warnings[0]

    def test_no_configured_skills_returns_no_rows(self, monkeypatch):
        self._downloaded(monkeypatch, [])
        self._mcp(monkeypatch, {}, {})

        assert sl._configured_skills({}) == []

    def test_empty_state_prints_a_note(self, monkeypatch):
        notes: list[str] = []
        monkeypatch.setattr(sl, "_configured_skills", lambda _state: [])
        monkeypatch.setattr(sl, "load_state", lambda: {})
        monkeypatch.setattr(sl, "print_note", notes.append)

        sl.list_configured_skills_command()

        assert notes and "ug skills add" in notes[0]
