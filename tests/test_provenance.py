"""Tests for the per-file provenance store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ucode.config_io as config_io
from ucode import managed_files, provenance


@pytest.fixture
def store_path() -> Path:
    return managed_files.MANAGED_BACKUP_DIR / "provenance.json"


class TestLoadSave:
    def test_load_returns_none_without_store(self):
        assert provenance.load_provenance("claude", Path("/tmp/a.json")) is None

    def test_round_trip_keeps_files_and_agents_independent(self):
        claude_path = Path("/etc/claude-code/managed-settings.json")
        codex_path = Path("/etc/codex/managed_config.toml")
        provenance.save_provenance(
            "claude", claude_path, {("env", "A"): "1", ("apiKeyHelper",): "/bin/helper"}
        )
        provenance.save_provenance("codex", codex_path, {("model_provider",): "Databricks"})

        assert provenance.load_provenance("claude", claude_path) == {
            ("env", "A"): "1",
            ("apiKeyHelper",): "/bin/helper",
        }
        assert provenance.load_provenance("codex", codex_path) == {
            ("model_provider",): "Databricks"
        }
        assert provenance.load_provenance("codex", claude_path) is None
        assert provenance.load_provenance("claude", codex_path) is None

    def test_save_replaces_prior_record_but_keeps_other_files(self):
        path_a = Path("/tmp/a.json")
        path_b = Path("/tmp/b.json")
        provenance.save_provenance("claude", path_a, {("x",): 1})
        provenance.save_provenance("claude", path_b, {("y",): 2})
        provenance.save_provenance("claude", path_a, {})

        assert provenance.load_provenance("claude", path_a) == {}
        assert provenance.load_provenance("claude", path_b) == {("y",): 2}

    def test_explicit_null_value_round_trips(self):
        path = Path("/tmp/a.json")
        provenance.save_provenance("claude", path, {("env", "A"): None})
        assert provenance.load_provenance("claude", path) == {("env", "A"): None}

    @pytest.mark.parametrize(
        "contents",
        [b"not json{", b"\xff\xfe{", json.dumps({"files": []}).encode(), None],
        ids=["corrupt", "invalid-utf8", "files-not-dict", "unreadable"],
    )
    def test_invalid_store_means_no_record(self, store_path, contents):
        if contents is None:
            store_path.mkdir(parents=True)
        else:
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.write_bytes(contents)
        assert provenance.load_provenance("claude", Path("/tmp/a.json")) is None

    def test_clear_without_a_store_leaves_the_backup_dir_untouched(self, store_path):
        assert not store_path.parent.exists()
        provenance.clear_provenance()
        assert not store_path.parent.exists()

    def test_clear_removes_the_store(self, store_path):
        provenance.save_provenance("claude", Path("/tmp/a.json"), {("x",): 1})
        provenance.clear_provenance()
        assert not store_path.exists()
        assert provenance.load_provenance("claude", Path("/tmp/a.json")) is None


class TestLocking:
    def test_save_holds_the_lock_across_read_and_write(self, monkeypatch):
        events: list[str] = []
        real_load = provenance._load_store
        real_write = managed_files._write_private_file

        def record_load():
            events.append("read")
            return real_load()

        def record_write(path, text):
            events.append("write")
            real_write(path, text)

        monkeypatch.setattr(
            provenance, "acquire_exclusive_file_lock", lambda _f: events.append("acquire")
        )
        monkeypatch.setattr(provenance, "release_file_lock", lambda _f: events.append("release"))
        monkeypatch.setattr(provenance, "_load_store", record_load)
        monkeypatch.setattr(managed_files, "_write_private_file", record_write)

        provenance.save_provenance("claude", Path("/tmp/a.json"), {("x",): 1})

        assert events == ["acquire", "read", "write", "release"]

    def test_clear_holds_the_lock_while_removing_the_store(self, monkeypatch, store_path):
        provenance.save_provenance("claude", Path("/tmp/a.json"), {("x",): 1})
        events: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            provenance,
            "acquire_exclusive_file_lock",
            lambda _f: events.append(("acquire", store_path.exists())),
        )
        monkeypatch.setattr(
            provenance,
            "release_file_lock",
            lambda _f: events.append(("release", store_path.exists())),
        )

        provenance.clear_provenance()

        assert events == [("acquire", True), ("release", False)]


class TestDryRun:
    @pytest.fixture(autouse=True)
    def _restore_dry_run(self):
        yield
        config_io.set_dry_run(False)

    def test_save_touches_nothing_and_takes_no_lock(self, monkeypatch, store_path):
        acquired: list[object] = []
        monkeypatch.setattr(provenance, "acquire_exclusive_file_lock", lambda f: acquired.append(f))
        config_io.set_dry_run(True)
        provenance.save_provenance("claude", Path("/tmp/a.json"), {("x",): 1})
        assert not store_path.exists()
        assert acquired == []

    def test_clear_does_not_delete_or_lock(self, monkeypatch, store_path):
        provenance.save_provenance("claude", Path("/tmp/a.json"), {("x",): 1})
        acquired: list[object] = []
        monkeypatch.setattr(provenance, "acquire_exclusive_file_lock", lambda f: acquired.append(f))
        config_io.set_dry_run(True)
        provenance.clear_provenance()
        assert store_path.exists()
        assert acquired == []


class TestOwnedAfterWrite:
    def test_generated_value_that_landed_is_owned(self):
        final = {"env": {"A": "1"}}
        assert provenance.owned_after_write({}, {("env", "A"): "1"}, final) == {("env", "A"): "1"}

    def test_owned_values_do_not_alias_the_written_document(self):
        deny = ["WebSearch"]
        final = {"permissions": {"deny": deny}}
        owned = provenance.owned_after_write({}, {("permissions", "deny"): deny}, final)
        deny.append("Bash(rm:*)")
        assert owned == {("permissions", "deny"): ["WebSearch"]}

    def test_generated_value_replaced_in_final_is_not_owned(self):
        final = {"env": {"A": "edited"}}
        assert provenance.owned_after_write({}, {("env", "A"): "1"}, final) == {}

    def test_generated_value_absent_in_final_is_not_owned(self):
        assert provenance.owned_after_write({}, {("env", "A"): "1"}, {"env": {}}) == {}

    def test_value_the_file_already_held_is_not_adopted(self):
        doc = {"env": {"A": "1"}}
        assert provenance.owned_after_write({}, {("env", "A"): "1"}, doc, before=doc) == {}

    def test_value_this_write_changed_is_adopted(self):
        before = {"env": {"A": "old"}}
        final = {"env": {"A": "1"}}
        owned = provenance.owned_after_write({}, {("env", "A"): "1"}, final, before=before)
        assert owned == {("env", "A"): "1"}

    def test_already_owned_unchanged_value_stays_owned_with_before(self):
        doc = {"env": {"A": "1"}}
        owned = provenance.owned_after_write(
            {("env", "A"): "1"}, {("env", "A"): "1"}, doc, before=doc
        )
        assert owned == {("env", "A"): "1"}

    def test_edit_that_matches_the_next_generated_value_stays_theirs(self):
        doc = {"model": "B"}
        owned = provenance.owned_after_write({("model",): "A"}, {("model",): "B"}, doc, before=doc)
        assert owned == {}

    def test_previously_owned_ungenerated_and_unchanged_is_kept(self):
        final = {"env": {"A": "1"}}
        assert provenance.owned_after_write({("env", "A"): "1"}, {}, final) == {("env", "A"): "1"}

    def test_previously_owned_ungenerated_and_changed_is_dropped(self):
        final = {"env": {"A": "edited"}}
        assert provenance.owned_after_write({("env", "A"): "1"}, {}, final) == {}

    def test_generated_value_wins_over_prior_owned_value(self):
        final = {"env": {"A": "new"}}
        assert provenance.owned_after_write(
            {("env", "A"): "old"}, {("env", "A"): "new"}, final
        ) == {("env", "A"): "new"}

    def test_failed_generated_write_keeps_the_prior_owned_value(self):
        final = {"env": {"A": "old"}}
        assert provenance.owned_after_write(
            {("env", "A"): "old"}, {("env", "A"): "new"}, final
        ) == {("env", "A"): "old"}

    def test_third_party_value_drops_ownership(self):
        final = {"env": {"A": "other"}}
        assert (
            provenance.owned_after_write({("env", "A"): "old"}, {("env", "A"): "new"}, final) == {}
        )

    def test_bool_is_not_interchangeable_with_int(self):
        assert provenance.owned_after_write({}, {("env", "A"): True}, {"env": {"A": 1}}) == {}
        assert provenance.owned_after_write({}, {("env", "A"): [True]}, {"env": {"A": [1]}}) == {}

    def test_explicit_null_is_a_present_value(self):
        generated = {("env", "A"): None}
        assert provenance.owned_after_write({}, generated, {"env": {"A": None}}) == generated
        assert provenance.owned_after_write({}, generated, {"env": {}}) == {}


class TestValuesAt:
    def test_picks_present_values(self):
        doc = {"env": {"A": "1"}, "apiKeyHelper": "/bin/helper"}
        owned = provenance.values_at([["env", "A"], ["apiKeyHelper"]], doc)
        assert owned == {("env", "A"): "1", ("apiKeyHelper",): "/bin/helper"}

    def test_skips_absent_paths(self):
        owned = provenance.values_at([["env", "A"], ["env", "B"], ["other"]], {"env": {"A": "1"}})
        assert owned == {("env", "A"): "1"}

    def test_none_doc_owns_nothing(self):
        assert provenance.values_at([["env", "A"]], None) == {}

    def test_values_do_not_alias_the_document(self):
        doc = {"permissions": {"deny": ["WebSearch"]}}
        owned = provenance.values_at([["permissions", "deny"]], doc)
        doc["permissions"]["deny"].append("Bash(rm:*)")
        assert owned == {("permissions", "deny"): ["WebSearch"]}

    def test_explicit_null_is_present(self):
        assert provenance.values_at([["env", "A"]], {"env": {"A": None}}) == {("env", "A"): None}


class TestRetire:
    def test_restores_baseline_value(self):
        doc = {"env": {"A": "ug"}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], {"env": {"A": "orig"}})
        assert doc == {"env": {"A": "orig"}}

    def test_deletes_without_baseline_and_prunes_empty_parents(self):
        doc = {"env": {"A": "ug"}, "other": 1}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], {})
        assert doc == {"other": 1}

    def test_none_baseline_deletes(self):
        doc = {"env": {"A": "ug"}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], None)
        assert doc == {}

    def test_keeps_edited_owned_value(self):
        doc = {"env": {"A": "edited"}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], {"env": {"A": "orig"}})
        assert doc == {"env": {"A": "edited"}}

    def test_leaves_unowned_paths_untouched(self):
        doc = {"env": {"A": "user"}}
        provenance.retire(doc, {}, [("env", "A")], {})
        assert doc == {"env": {"A": "user"}}

    def test_deleted_owned_value_counts_as_changed(self):
        doc: dict = {"env": {}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], {"env": {"A": "orig"}})
        assert doc == {"env": {}}

    def test_explicit_null_baseline_is_restored_as_null(self):
        doc = {"env": {"A": "ug"}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], {"env": {"A": None}})
        assert doc == {"env": {"A": None}}

    def test_bool_owned_value_against_int_live_value_counts_as_edited(self):
        doc = {"flag": 1}
        provenance.retire(doc, {("flag",): True}, [("flag",)], {"flag": False})
        assert doc == {"flag": 1}

    def test_nested_bool_list_against_int_list_counts_as_edited(self):
        doc = {"flags": [1]}
        provenance.retire(doc, {("flags",): [True]}, [("flags",)], {})
        assert doc == {"flags": [1]}

    def test_restored_value_does_not_alias_the_baseline(self):
        baseline = {"env": {"A": {"nested": [1]}}}
        doc = {"env": {"A": "ug"}}
        provenance.retire(doc, {("env", "A"): "ug"}, [("env", "A")], baseline)
        doc["env"]["A"]["nested"].append(2)
        assert baseline == {"env": {"A": {"nested": [1]}}}


class TestRetireGroup:
    PATHS = [("env", "A"), ("env", "B")]

    def test_all_owned_members_unchanged_retires_all(self):
        doc = {"env": {"A": "ug", "B": "ug"}}
        owned = {("env", "A"): "ug", ("env", "B"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {"env": {"A": "orig"}})
        assert doc == {"env": {"A": "orig"}}

    def test_one_edited_member_blocks_the_whole_group(self):
        doc = {"env": {"A": "ug", "B": "edited"}}
        owned = {("env", "A"): "ug", ("env", "B"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {"env": {"A": "orig", "B": "orig-b"}})
        assert doc == {"env": {"A": "ug", "B": "edited"}}

    def test_deleted_owned_member_blocks_the_whole_group(self):
        doc = {"env": {"A": "ug"}}
        owned = {("env", "A"): "ug", ("env", "B"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {"env": {"A": "orig", "B": "orig-b"}})
        assert doc == {"env": {"A": "ug"}}
        assert owned == {}

    def test_bool_member_against_int_live_value_blocks_the_group(self):
        doc = {"env": {"A": 1, "B": "ug"}}
        owned = {("env", "A"): True, ("env", "B"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {})
        assert doc == {"env": {"A": 1, "B": "ug"}}

    def test_present_unowned_member_blocks_the_group(self):
        doc = {"env": {"A": "ug", "B": "user"}}
        owned = {("env", "A"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {})
        assert doc == {"env": {"A": "ug", "B": "user"}}
        assert owned == {}

    def test_unowned_member_still_at_its_baseline_does_not_block(self):
        doc = {"env": {"A": "ug", "B": "1"}}
        provenance.retire_group(
            doc, {("env", "A"): "ug"}, self.PATHS, {"env": {"A": "orig", "B": "1"}}
        )
        assert doc == {"env": {"A": "orig", "B": "1"}}

    def test_absent_unowned_member_does_not_block(self):
        doc = {"env": {"A": "ug"}}
        provenance.retire_group(doc, {("env", "A"): "ug"}, self.PATHS, {})
        assert doc == {}

    def test_veto_forgets_the_group_so_a_later_run_keeps_the_remnant(self):
        doc = {"env": {"A": "ug", "B": "edited"}}
        owned = {("env", "A"): "ug", ("env", "B"): "ug"}
        provenance.retire_group(doc, owned, self.PATHS, {})
        assert owned == {}
        provenance.retire_group(doc, provenance.owned_after_write(owned, {}, doc), self.PATHS, {})
        assert doc == {"env": {"A": "ug", "B": "edited"}}
