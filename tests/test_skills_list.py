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


class TestListConfiguredSkills:
    @pytest.fixture
    def captured(self, monkeypatch):
        calls: dict = {"rows": None, "note": None}
        monkeypatch.setattr(sl, "render_box_table", lambda headers, rows: calls.update(rows=rows))
        monkeypatch.setattr(sl, "print_note", lambda msg: calls.update(note=msg))
        monkeypatch.setattr(sl, "console", type("C", (), {"print": staticmethod(lambda *a: None)}))
        monkeypatch.setattr(sl, "load_state", lambda: {})
        monkeypatch.setattr(sl, "get_databricks_token", lambda *a, **k: "token")
        return calls

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

    def test_downloaded_only_is_visible_to_all_agents(self, monkeypatch, captured):
        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(monkeypatch, {}, {})

        sl.list_configured_skills_command()

        assert captured["rows"] == [["alpha", "main.default", "downloaded", "all"]]

    def test_mcp_scope_expands_to_named_skills_for_its_agents(self, monkeypatch, captured):
        self._downloaded(monkeypatch, [])
        self._mcp(
            monkeypatch,
            {"claude": ["main.default"], "codex": ["main.default"]},
            {"main.default": ([ref("alpha")], None)},
        )

        sl.list_configured_skills_command()

        assert captured["rows"] == [["alpha", "main.default", "skill mcp", "claude,codex"]]

    def test_downloaded_and_scoped_is_flagged_as_both(self, monkeypatch, captured):
        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(
            monkeypatch, {"claude": ["main.default"]}, {"main.default": ([ref("alpha")], None)}
        )

        sl.list_configured_skills_command()

        assert captured["rows"] == [["alpha", "main.default", "both (discouraged)", "all"]]

    def test_sorted_by_name_then_location(self, monkeypatch, captured):
        self._downloaded(monkeypatch, ["z.a.beta", "main.default.alpha", "ml.prod.alpha"])
        self._mcp(monkeypatch, {}, {})

        sl.list_configured_skills_command()

        assert [row[:2] for row in captured["rows"]] == [
            ["alpha", "main.default"],
            ["alpha", "ml.prod"],
            ["beta", "z.a"],
        ]

    def test_repeated_installs_of_one_skill_collapse_to_one_row(self, monkeypatch, captured):
        records = [
            {"fqn": "main.default.alpha", "bundle_name": "alpha", "base": "/home/me"},
            {"fqn": "main.default.alpha", "bundle_name": "alpha", "base": "/work/proj"},
        ]
        monkeypatch.setattr(sl, "list_downloaded", lambda: records)
        self._mcp(monkeypatch, {}, {})

        sl.list_configured_skills_command()

        assert captured["rows"] == [["alpha", "main.default", "downloaded", "all"]]

    def test_no_configured_skills_prints_a_note(self, monkeypatch, captured):
        self._downloaded(monkeypatch, [])
        self._mcp(monkeypatch, {}, {})

        sl.list_configured_skills_command()

        assert captured["rows"] is None
        assert "ug skills add" in captured["note"]

    def test_failed_schema_listing_keeps_the_scope_with_a_placeholder(self, monkeypatch, captured):
        warnings: list[str] = []
        monkeypatch.setattr(sl, "print_warning", warnings.append)
        self._downloaded(monkeypatch, [])
        self._mcp(
            monkeypatch, {"claude": ["main.default"]}, {"main.default": ([], "HTTP 404 Not Found")}
        )

        sl.list_configured_skills_command()

        assert captured["rows"] == [
            ["(skills in main.default)", "main.default", "skill mcp", "claude"]
        ]
        assert warnings and "main.default" in warnings[0]
