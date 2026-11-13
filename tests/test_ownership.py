"""Ownership tables: loading, the four-layer merge, docs freshness, and parity with the agents' code.

The parity tests are the CI check that keeps the tables current: a key the agent code writes or
strips must have the matching class in the table, and the generated admin docs must match it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ucode import ownership
from ucode.agents import claude, codex
from ucode.ownership import __main__ as ownership_cli

DOCS = Path(__file__).resolve().parents[1] / "docs" / "admin-managed-settings-reference.md"


def _claude_entries() -> dict[str, ownership.Entry]:
    return {
        entry.dotted: entry for entry in ownership.load_table("claude").section("managed_settings")
    }


def _codex_entries(section: str) -> dict[str, ownership.Entry]:
    return {entry.dotted: entry for entry in ownership.load_table("codex").section(section)}


class TestTables:
    def test_tables_load_and_validate(self):
        for agent in ownership.AGENTS:
            table = ownership.load_table(agent)
            for name, entries in table.sections.items():
                assert entries, f"{agent}.{name} is empty"

    def test_sections_are_the_native_config_fields(self):
        assert list(ownership.load_table("claude").sections) == ["managed_settings"]
        assert list(ownership.load_table("codex").sections) == ["requirements", "config"]

    def test_unknown_class_is_rejected(self):
        with pytest.raises(ValueError, match="unknown class"):
            ownership._parse_entry({"path": "x", "class": "owned", "note": "n"}, source="t")

    def test_conditional_entry_needs_a_feature(self):
        with pytest.raises(ValueError, match="needs a feature"):
            ownership._parse_entry(
                {"path": "x", "class": "conditionally_reserved", "note": "n"}, source="t"
            )

    def test_default_needs_a_value(self):
        with pytest.raises(ValueError, match="needs a value"):
            ownership._parse_entry({"path": "x", "class": "default", "note": "n"}, source="t")


class TestDocsAreGenerated:
    """CI: the admin docs' tables are regenerated whenever a table changes."""

    def test_docs_section_matches_tables(self):
        text = DOCS.read_text(encoding="utf-8")
        assert ownership.replace_docs_section(text) == text, (
            "docs/admin-managed-settings-reference.md is stale. Run "
            "`uv run python -m ucode.ownership --write docs/admin-managed-settings-reference.md`."
        )

    def test_check_command_fails_on_stale_docs(self, tmp_path, capsys):
        stale = tmp_path / "docs.md"
        stale.write_text(
            f"intro\n{ownership.DOCS_BEGIN}\nold\n{ownership.DOCS_END}\n", encoding="utf-8"
        )
        assert ownership_cli.main(["--check", str(stale)]) == 1
        assert "out of date" in capsys.readouterr().err
        assert ownership_cli.main(["--write", str(stale)]) == 0
        assert ownership_cli.main(["--check", str(stale)]) == 0
        assert stale.read_text(encoding="utf-8").startswith("intro\n")


class TestClaudeParity:
    """CI: the Claude table agrees with the constants the non-MDM code paths use."""

    def test_gateway_keys_are_reserved_and_written(self):
        entries = _claude_entries()
        overlay, keys = claude.render_overlay("https://example.cloud.databricks.com", None)
        defaults = ownership.defaults(ownership.load_table("claude").section("managed_settings"))
        for path in keys:
            dotted = ".".join(path)
            if dotted in defaults:
                continue
            assert dotted in entries, f"render_overlay writes {dotted}, which the table lacks"
            assert entries[dotted].cls == ownership.RESERVED
            assert entries[dotted].in_managed_file

    def test_defaults_match_render_overlay(self):
        overlay, _keys = claude.render_overlay("https://example.cloud.databricks.com", None)
        for dotted, value in ownership.defaults(
            ownership.load_table("claude").section("managed_settings")
        ).items():
            section, key = dotted.split(".", 1)
            assert overlay[section][key] == value, f"{dotted} default differs from render_overlay"

    def test_trace_keys_are_reserved_while_tracing(self):
        entries = _claude_entries()
        for path in claude.CLAUDE_OTEL_TRACE_PATHS:
            entry = entries[".".join(path)]
            assert (entry.cls, entry.feature) == (ownership.CONDITIONALLY_RESERVED, "tracing")

    def test_picker_keys_are_reserved_while_models_are_set(self):
        entries = _claude_entries()
        for key in claude.CLAUDE_MANAGED_PICKER_KEYS:
            assert (entries[key].cls, entries[key].feature) == (
                ownership.CONDITIONALLY_RESERVED,
                "models",
            )

    def test_model_keys_are_reserved(self):
        entries = _claude_entries()
        for key in claude.CLAUDE_MANAGED_MODEL_ENV_KEYS:
            entry = entries[f"env.{key}"]
            assert entry.cls in (ownership.RESERVED, ownership.CONDITIONALLY_RESERVED)
            assert entry.in_managed_file == (key in claude.CLAUDE_DEFAULT_MODEL_ENV_KEYS.values())

    def test_launch_and_removed_keys_never_reach_the_managed_file(self):
        entries = _claude_entries()
        for key in (*claude.CLAUDE_CONDITIONAL_ENV_KEYS, *claude.CLAUDE_REMOVED_ENV_KEYS):
            assert entries[f"env.{key}"].cls == ownership.RESERVED
            assert not entries[f"env.{key}"].in_managed_file

    def test_credential_blanks_and_mcp_keys_are_reserved(self):
        entries = _claude_entries()
        for path in claude.CLAUDE_BLANKED_CREDENTIAL_PATHS:
            assert entries[".".join(path)].cls == ownership.RESERVED
        assert entries[claude.MANAGED_MCP_SETTINGS_KEY].cls == ownership.RESERVED
        assert entries[claude.CLAUDE_ALLOWED_MCP_SERVERS_KEY].cls == ownership.SHARED_LIST
        assert entries[".".join(claude.CLAUDE_PERMISSIONS_DENY_PATH)].cls == ownership.SHARED_LIST


class TestCodexParity:
    def test_provider_is_reserved_in_both_files(self):
        requirements = _codex_entries("requirements")
        settings = _codex_entries("config")
        assert requirements["model_provider"].cls == ownership.RESERVED
        assert requirements["model_providers"].cls == ownership.RESERVED
        assert settings[f"model_providers.{codex.CODEX_MODEL_PROVIDER_NAME}"].cls == (
            ownership.RESERVED
        )
        assert not settings["model_provider"].in_managed_file

    def test_hook_lockdowns_are_reserved_while_smart_routing(self):
        requirements = _codex_entries("requirements")
        for key in ("allow_managed_hooks_only", "features.hooks"):
            assert requirements[key].feature == "smart_routing"


class TestCompose:
    ENTRIES = (
        ownership.Entry(("env", "BASE"), ownership.RESERVED, "gateway"),
        ownership.Entry(("env", "CACHE"), ownership.DEFAULT, "cache", value="1"),
        ownership.Entry(
            ("env", "TRACE"), ownership.CONDITIONALLY_RESERVED, "trace", feature="tracing"
        ),
        ownership.Entry(("never",), ownership.RESERVED, "launch", in_managed_file=False),
        ownership.Entry(("permissions", "deny"), ownership.SHARED_LIST, "deny"),
        ownership.Entry(("mcp",), ownership.SHARED_TABLE, "mcp"),
    )

    def test_layers_and_report(self):
        composed = ownership.compose(
            self.ENTRIES,
            native={
                "env": {"BASE": "evil", "CACHE": "0", "TRACE": "mine", "OTHER": "x"},
                "never": True,
                "permissions": {"deny": ["Bash(rm *)", "WebSearch"]},
                "companyAnnouncements": ["hi"],
            },
            ug_values={"env": {"BASE": "gw"}, "permissions": {"deny": ["WebSearch"]}},
            features=set(),
        )
        assert composed.doc == {
            "env": {"BASE": "gw", "CACHE": "0", "TRACE": "mine", "OTHER": "x"},
            "permissions": {"deny": ["Bash(rm *)", "WebSearch"]},
            "companyAnnouncements": ["hi"],
        }
        outcomes = {item.path: item.outcome for item in composed.items}
        assert outcomes["env.BASE"] == ownership.SKIPPED
        assert outcomes["env.CACHE"] == ownership.OVERRODE_DEFAULT
        assert outcomes["env.TRACE"] == ownership.APPLIED
        assert outcomes["never"] == ownership.SKIPPED

    def test_feature_on_takes_the_key(self):
        composed = ownership.compose(
            self.ENTRIES,
            native={"env": {"TRACE": "mine"}},
            ug_values={"env": {"TRACE": "ug"}},
            features={"tracing"},
        )
        assert composed.doc["env"]["TRACE"] == "ug"
        assert composed.items[0].outcome == ownership.SKIPPED

    def test_default_written_when_admin_is_silent(self):
        composed = ownership.compose(self.ENTRIES, native=None, ug_values={}, features=set())
        assert composed.doc == {"env": {"CACHE": "1"}}

    def test_whole_object_counts_as_every_key_inside(self):
        composed = ownership.compose(
            self.ENTRIES, native={"env": None}, ug_values={"env": {"BASE": "gw"}}, features=set()
        )
        assert composed.doc["env"] == {"BASE": "gw", "CACHE": "1"}
        assert composed.items[0].outcome == ownership.SKIPPED

    def test_replacing_a_shared_parent_is_skipped(self):
        composed = ownership.compose(
            self.ENTRIES,
            native={"permissions": "everything"},
            ug_values={"permissions": {"deny": ["WebSearch"]}},
            features=set(),
        )
        assert composed.doc["permissions"] == {"deny": ["WebSearch"]}

    def test_shared_table_drops_admin_entries_with_ug_names(self):
        composed = ownership.compose(
            self.ENTRIES,
            native={"mcp": {"jira": {"url": "j"}, "github": {"url": "evil"}}},
            ug_values={"mcp": {"github": {"url": "ug"}}},
            features=set(),
            ug_table_names={("mcp",): {"github"}},
        )
        assert composed.doc["mcp"] == {"jira": {"url": "j"}, "github": {"url": "ug"}}

    def test_a_dropped_shared_table_entry_is_reported_once(self):
        composed = ownership.compose(
            self.ENTRIES,
            native={"mcp": {"github": {"url": "evil", "headers": {"X": "1"}}}},
            ug_values={"mcp": {"github": {"url": "ug"}}},
            features=set(),
            ug_table_names={("mcp",): {"github"}},
        )
        assert [item.path for item in composed.items] == ["mcp.github"]

    def test_ug_may_not_write_keys_the_table_does_not_reserve(self):
        with pytest.raises(ValueError, match="ownership table"):
            ownership.compose(self.ENTRIES, native=None, ug_values={"unlisted": 1}, features=set())
        with pytest.raises(ValueError, match="ownership table"):
            ownership.compose(self.ENTRIES, native=None, ug_values={"never": 1}, features=set())
        with pytest.raises(ValueError, match="ownership table"):
            ownership.compose(
                self.ENTRIES, native=None, ug_values={"env": {"TRACE": "x"}}, features=set()
            )
