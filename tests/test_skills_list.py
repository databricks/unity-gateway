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

    def test_sorted_by_location_then_name(self, monkeypatch):
        self._downloaded(monkeypatch, ["a.a.zeta", "shared.s.gamma", "shared.s.alpha"])
        self._mcp(monkeypatch, {}, {})

        assert [(r.name, r.location) for r in sl._configured_skills({})] == [
            ("zeta", "a.a"),
            ("alpha", "shared.s"),
            ("gamma", "shared.s"),
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


class TestConfiguredSkillCountsByAgent:
    _AGENTS = ("claude", "codex", "gemini")

    def _downloaded(self, monkeypatch, fqns):
        monkeypatch.setattr(sl, "list_downloaded", lambda: [{"fqn": fqn} for fqn in fqns])

    def _mcp(self, monkeypatch, locations_by_client, by_schema):
        monkeypatch.setattr(sl, "get_databricks_token", lambda *a, **k: "token")
        monkeypatch.setattr(
            sl,
            "configured_skill_workspace_and_mcp_locations",
            lambda _state: (WS, locations_by_client) if locations_by_client else None,
        )
        monkeypatch.setattr(
            sl, "list_schema_skills", lambda ws, tok, c, s: by_schema.get(f"{c}.{s}", ([], None))
        )

    def test_downloaded_skills_reach_every_agent(self, monkeypatch):
        self._downloaded(monkeypatch, ["main.default.alpha", "main.default.beta"])
        self._mcp(monkeypatch, {}, {})

        assert sl.configured_skill_counts_by_agent({}, self._AGENTS) == {
            "claude": 2,
            "codex": 2,
            "gemini": 2,
        }

    def test_combines_local_and_per_agent_mcp_skills(self, monkeypatch):
        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(
            monkeypatch,
            {"claude": ["main.default", "ml.prod"], "codex": ["main.default"]},
            {
                "main.default": ([ref("alpha"), ref("beta")], None),
                "ml.prod": ([ref("gamma", catalog="ml", schema="prod")], None),
            },
        )

        assert sl.configured_skill_counts_by_agent({}, self._AGENTS) == {
            "claude": 3,  # alpha (local + mcp), beta, gamma
            "codex": 2,  # alpha (local + mcp), beta
            "gemini": 1,  # alpha (local only)
        }

    def test_unreadable_schema_is_skipped(self, monkeypatch):
        self._downloaded(monkeypatch, [])
        self._mcp(
            monkeypatch,
            {"claude": ["main.default", "ml.prod"]},
            {"main.default": ([ref("alpha")], None), "ml.prod": ([], "HTTP 403 Forbidden")},
        )

        assert sl.configured_skill_counts_by_agent({}, ["claude"]) == {"claude": 1}

    def test_token_error_yields_local_only(self, monkeypatch):
        def _raise(*_a, **_k):
            raise RuntimeError("no token")

        self._downloaded(monkeypatch, ["main.default.alpha"])
        self._mcp(monkeypatch, {"claude": ["main.default", "ml.prod"]}, {})
        monkeypatch.setattr(sl, "get_databricks_token", _raise)

        assert sl.configured_skill_counts_by_agent({}, ["claude"]) == {"claude": 1}

    def test_agent_without_skills_counts_zero(self, monkeypatch):
        self._downloaded(monkeypatch, [])
        self._mcp(monkeypatch, {}, {})

        assert sl.configured_skill_counts_by_agent({}, ["claude"]) == {"claude": 0}


class TestAvailableSkills:
    """`ug skills list --available` lists what UC offers and marks what is configured."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.rendered: list[list[sl.AvailableSkill]] = []

        def render(rows, _width):
            self.rendered.append(rows)
            return ["HEADER"]

        monkeypatch.setattr(sl, "get_databricks_token", lambda *a, **k: "token")
        monkeypatch.setattr(sl, "load_state", lambda: {"workspace": WS, "profile": "p"})
        monkeypatch.setattr(sl, "list_downloaded", lambda: [])
        monkeypatch.setattr(sl, "configured_skill_workspace_and_mcp_locations", lambda _s: None)
        monkeypatch.setattr(sl, "print_note", self.notes.append)
        monkeypatch.setattr(sl, "print_warning", self.warnings.append)
        monkeypatch.setattr(sl, "print_heading", lambda _text: None)
        monkeypatch.setattr(sl.console, "print", lambda *a, **k: None)
        monkeypatch.setattr(sl, "_render_available", render)

    def _rows(self):
        assert len(self.rendered) == 1
        return [(r.name, r.location, r.configured, r.description) for r in self.rendered[0]]

    def test_without_location_walks_the_workspace(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def list_all(ws, tok):
            calls.append((ws, tok))
            return [ref("alpha")], None

        monkeypatch.setattr(sl, "list_all_skills", list_all)
        monkeypatch.setattr(
            sl, "list_schema_skills", lambda *a: pytest.fail("must walk, not list one schema")
        )

        assert sl.list_available_skills_command([]) == 0

        assert calls == [(WS, "token")]
        assert self._rows() == [("alpha", "main.default", "-", "")]

    def test_location_lists_only_those_schemas(self, monkeypatch):
        listed: list[str] = []

        def list_schema(ws, tok, catalog, schema):
            listed.append(f"{catalog}.{schema}")
            return [ref("beta", catalog=catalog, schema=schema)], None

        monkeypatch.setattr(sl, "list_schema_skills", list_schema)
        monkeypatch.setattr(sl, "list_all_skills", lambda *a: pytest.fail("must not walk"))

        sl.list_available_skills_command(["ml.prod", "main.default"])

        assert listed == ["ml.prod", "main.default"]
        assert [row[:2] for row in self._rows()] == [
            ("beta", "main.default"),
            ("beta", "ml.prod"),
        ]

    def test_marks_how_each_skill_is_configured(self, monkeypatch):
        monkeypatch.setattr(
            sl, "list_downloaded", lambda: [{"fqn": "main.default.alpha"}, {"fqn": "ml.prod.gamma"}]
        )
        monkeypatch.setattr(
            sl,
            "configured_skill_workspace_and_mcp_locations",
            lambda _s: (WS, {"claude": ["main.default"], "codex": []}),
        )
        refs = [
            ref("alpha"),
            ref("beta"),
            ref("gamma", catalog="ml", schema="prod"),
            ref("delta", catalog="x", schema="y"),
        ]
        monkeypatch.setattr(sl, "list_all_skills", lambda *a: (refs, None))

        sl.list_available_skills_command([])

        assert [(row[0], row[2]) for row in self._rows()] == [
            ("alpha", "local,mcp"),
            ("beta", "mcp"),
            ("gamma", "local"),
            ("delta", "-"),
        ]

    def test_description_is_collapsed_to_one_line(self, monkeypatch):
        skill = SkillRef(
            catalog="main",
            schema="default",
            securable_name="alpha",
            bundle_name="alpha",
            description="Use for SQL.\n  Covers   joins.",
        )
        monkeypatch.setattr(sl, "list_all_skills", lambda *a: ([skill], None))

        sl.list_available_skills_command([])

        assert self._rows()[0][3] == "Use for SQL. Covers joins."

    def test_unreadable_schema_is_warned_and_skipped(self, monkeypatch):
        by_schema = {"main.default": ([ref("alpha")], None), "ml.prod": ([], "HTTP 403 Forbidden")}
        monkeypatch.setattr(sl, "list_schema_skills", lambda ws, tok, c, s: by_schema[f"{c}.{s}"])

        sl.list_available_skills_command(["main.default", "ml.prod"])

        assert [row[0] for row in self._rows()] == ["alpha"]
        assert len(self.warnings) == 1 and "ml.prod" in self.warnings[0]

    def test_walk_timeout_shows_partial_results(self, monkeypatch):
        monkeypatch.setattr(
            sl, "list_all_skills", lambda *a: ([ref("alpha")], sl._SKILLS_WALK_TIMEOUT_REASON)
        )

        sl.list_available_skills_command([])

        assert [row[0] for row in self._rows()] == ["alpha"]
        assert len(self.warnings) == 1 and "--location" in self.warnings[0]

    def test_walk_error_raises(self, monkeypatch):
        monkeypatch.setattr(sl, "list_all_skills", lambda *a: ([], "HTTP 401 Unauthorized"))

        with pytest.raises(RuntimeError, match="HTTP 401"):
            sl.list_available_skills_command([])

    def test_no_skills_prints_a_note(self, monkeypatch):
        monkeypatch.setattr(sl, "list_all_skills", lambda *a: ([], "no skills found"))

        assert sl.list_available_skills_command([]) == 0

        assert self.notes == ["No skills found in every schema you can read."]
        assert self.rendered == []

    def test_requires_a_configured_workspace(self, monkeypatch):
        monkeypatch.setattr(sl, "load_state", lambda: {})

        with pytest.raises(RuntimeError, match="ug configure"):
            sl.list_available_skills_command([])


class TestRenderAvailable:
    """One line per skill: NAME and LOCATION whole, DESCRIPTION fitted to the terminal."""

    LONG_LOCATION = "platform_tools.release_oncall_plugins_marketplace_experimental"
    LONG_DESCRIPTION = "Shows unified release pipeline status for every pipeline. " * 4

    def _rows(self):
        return [
            sl.AvailableSkill("release-status", self.LONG_LOCATION, "-", self.LONG_DESCRIPTION),
            sl.AvailableSkill("alpha", "main.default", "local,mcp", ""),
        ]

    def test_wide_terminal_fits_the_description_to_the_width(self):
        header, *lines = sl._render_available(self._rows(), 140)

        assert header.split() == ["NAME", "LOCATION", "CONFIGURED", "DESCRIPTION"]
        assert all(len(line) <= 140 for line in [header, *lines])
        assert lines[0].endswith("…") and len(lines[0]) == 140
        # Columns line up: every LOCATION starts where its header does.
        start = header.index("LOCATION")
        assert lines[0][start:].startswith(self.LONG_LOCATION)
        assert lines[1][start:].startswith("main.default")
        assert lines[1].endswith("local,mcp")  # empty description: no trailing padding

    def test_narrow_terminal_keeps_name_and_location_whole(self):
        _, *lines = sl._render_available(self._rows(), 60)

        assert lines[0].startswith("release-status")
        assert self.LONG_LOCATION in lines[0]
        description = lines[0].rsplit("    ", 1)[1]
        assert len(description) == sl._MIN_DESCRIPTION_WIDTH and description.endswith("…")

    def test_piped_output_keeps_every_description_whole(self):
        _, *lines = sl._render_available(self._rows(), None)

        assert lines[0].endswith(self.LONG_DESCRIPTION.strip())
