"""Ownership transactions on isolated real files, with no authentication or agents."""

from __future__ import annotations

import json
import multiprocessing
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode import managed_files as files
from ucode import managed_ownership as ownership
from ucode.agents import claude, codex
from ucode.config_io import deep_merge_dict
from ucode.managed_source import SelectedManagedSource, validate_file_config
from ucode.native_settings import compose_native_settings, native_ownership


def source(agent="claude", *, owner=None, workspace="https://a.example", handoff=None):
    manifest = {"enabled_agents": {"claude": {}, "codex": {}}}
    if owner is not None or handoff is not None:
        manifest["handoff"] = handoff or {
            "schema_version": 1,
            "owner": owner,
            "migration_version": 1,
            "agents": {},
        }
    return SelectedManagedSource(
        kind="file",
        workspace=workspace,
        agent=agent,
        resolved_path=Path("/input/a.json"),
        digest="input-digest",
        _manifest_json=json.dumps(manifest),
    )


def plan(path, overlay, target="private_settings", **kwargs):
    return ownership.DestinationPlan(
        target,
        path,
        json.loads,
        lambda doc: json.dumps(doc, indent=2) + "\n",
        lambda base: deep_merge_dict(base, deepcopy(overlay)),
        ownership.leaf_paths(overlay),
        **kwargs,
    )


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def get(path):
    return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def destinations(tmp_path, monkeypatch):
    paths = {
        "claude": tmp_path / "claude" / "private.json",
        "user": tmp_path / "claude" / "settings.json",
        "managed": tmp_path / "etc" / "claude.json",
        "codex": tmp_path / "codex" / "private.toml",
    }
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", paths["claude"])
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", paths["user"])
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: paths["managed"])
    monkeypatch.setattr(codex, "CODEX_CONFIG_PATH", paths["codex"])
    monkeypatch.setattr(files, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(
        files, "_SUDO_REPLACE_TARGETS", {files.current_os(): frozenset({paths["managed"]})}
    )
    monkeypatch.setattr(
        files,
        "_sudo_replace",
        lambda path, text, *, expected_text=None: ownership._atomic_replace(
            path, text, expected_text
        ),
    )
    return paths


def test_equal_value_adoption_then_omission_removes_owned_drift(destinations, monkeypatch):
    path = destinations["claude"]
    put(path, {"env": {"OWNED": "secret-value", "PERSONAL": "keep"}})
    writes = Mock(wraps=ownership._atomic_replace)
    monkeypatch.setattr(ownership, "_atomic_replace", writes)
    ownership.apply_source(source(), [plan(path, {"env": {"OWNED": "secret-value"}})])
    writes.assert_not_called()
    entry = files._find_entry(files._load_manifest(), "claude", path)
    assert entry["active_effects"] == [{"path": ["env", "OWNED"], "value": "secret-value"}]
    original_hash = entry["original_sha256"]
    put(path, {"env": {"OWNED": "edited-secret", "PERSONAL": "new-user-value"}})
    messages = []
    monkeypatch.setattr(ownership, "print_warning", messages.append)
    ownership.apply_source(source(), [plan(path, {})])
    assert get(path) == {"env": {"PERSONAL": "new-user-value"}}
    assert "env.OWNED" in " ".join(messages)
    assert "secret" not in " ".join(messages)
    assert (
        files._find_entry(files._load_manifest(), "claude", path)["original_sha256"]
        == original_hash
    )


@pytest.mark.parametrize(
    "first,second",
    [
        ({"env": {"TOKEN": "a"}}, {"env": {"TOKEN": "b"}}),
        ({"otel": {"endpoint": "a"}}, {"otel": {"endpoint": "b"}}),
        ({"features": {"fast": True}}, {"features": {"fast": False}}),
    ],
)
def test_add_change_remove_preserves_unrelated_siblings(destinations, first, second):
    path = destinations["claude"]
    put(path, {"personal": "keep"})
    for overlay in (first, second, {}):
        ownership.apply_source(source(), [plan(path, overlay)])
        assert get(path) == {"personal": "keep", **overlay}


def test_workspace_and_input_path_transitions_track_actual_applied_source(destinations):
    path = destinations["claude"]
    for workspace, filename in [("a", "one"), ("b", "two"), ("a", "three")]:
        selected = replace(
            source(workspace=f"https://{workspace}.example"),
            resolved_path=Path(f"/input/{filename}.json"),
        )
        ownership.apply_source(selected, [plan(path, {"endpoint": selected.workspace})])
        current = ownership.applied_source("claude")
        assert current["path"] == str(selected.resolved_path)
        assert current["workspace"] == selected.workspace
        assert get(path)["endpoint"] == selected.workspace
    assert len(files._load_manifest()["files"]) == 1


def test_agents_and_transferred_owner_release_are_independent(destinations):
    a, b = destinations["claude"], destinations["codex"]
    ownership.apply_source(source(owner="old"), [plan(a, {"a": 1})])
    ownership.apply_source(source("codex"), [plan(b, {"b": 2})])
    ownership.apply_source(source(owner="new"), [plan(a, {"a": 3})])
    ownership.release_owner("old", "claude")
    assert get(a) == {"a": 3}
    assert ownership.applied_source("codex")["status"] == "applied"
    assert get(b) == {"b": 2}
    ownership.release_owner("new", "claude")
    ownership.release_owner("new", "claude")
    assert get(a) == {}
    assert get(b) == {"b": 2}


def test_counted_array_contributions_preserve_equal_unowned_occurrences(destinations):
    path = destinations["claude"]
    put(path, {"allow": ["same", "same", True, 1]})
    for _ in range(3):
        ownership.apply_source(source(), [plan(path, {"allow": ["same", True]})])
        assert get(path)["allow"].count("same") == 2
        assert len(get(path)["allow"]) == 4
    ownership.apply_source(source(), [plan(path, {})])
    assert get(path)["allow"] == ["same", 1]
    assert type(get(path)["allow"][1]) is int


def test_hook_handler_added_to_owned_matcher_survives_cleanup(destinations):
    path = destinations["claude"]
    owned = {"matcher": "Bash", "hooks": [{"type": "command", "command": "owned"}]}
    overlay = {"hooks": {"PreToolUse": [owned]}}
    ownership.apply_source(source(), [plan(path, overlay)])
    live = get(path)
    live["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": "personal"})
    put(path, live)
    ownership.apply_source(source(), [plan(path, overlay)])
    ownership.apply_source(source(), [plan(path, {})])
    assert get(path) == {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "personal"}]}
            ]
        }
    }


def handoff(action, path, **extra):
    return {
        "schema_version": 1,
        "owner": "integration",
        "migration_version": 1,
        "agents": {"claude": {action: [{"target": "private_settings", "path": path, **extra}]}},
    }


def test_whole_array_adoption_conflict_precedes_any_write(destinations):
    path = destinations["claude"]
    put(path, {"allow": ["personal", "managed"]})
    selected = source(handoff=handoff("adopt", ["allow"]))
    with pytest.raises(RuntimeError, match="explicit elements"):
        ownership.apply_source(selected, [plan(path, {"allow": ["managed"]})])
    assert get(path) == {"allow": ["personal", "managed"]}
    assert ownership.applied_source("claude") is None


def native_plan(path, native, *, tool="claude"):
    paths, contributions, exact = native_ownership(tool, native)
    return ownership.DestinationPlan(
        "private_settings",
        path,
        json.loads,
        lambda doc: json.dumps(doc) + "\n",
        lambda base: compose_native_settings(tool, base, native, target="private_settings"),
        paths,
        contributions=contributions,
        exact_array_paths=exact,
    )


@pytest.mark.parametrize("incoming", [[], ["new"], ["unowned-secret"]])
def test_exact_native_array_requires_explicit_handoff_before_any_write(destinations, incoming):
    path = destinations["claude"]
    put(path, {"companyAnnouncements": ["unowned-secret"]})
    with pytest.raises(RuntimeError, match="explicit adopt or retire.*migration_version") as error:
        ownership.apply_source(source(), [native_plan(path, {"companyAnnouncements": incoming})])
    assert "unowned-secret" not in str(error.value)
    assert get(path) == {"companyAnnouncements": ["unowned-secret"]}
    assert ownership.applied_source("claude") is None


def test_exact_array_adoption_preserves_declared_order_and_detects_later_user_elements(
    destinations,
):
    path = destinations["claude"]
    put(path, {"companyAnnouncements": ["first", "second"]})
    selected = source(
        handoff=handoff("adopt", ["companyAnnouncements"], elements=["second", "first"])
    )
    native = {"companyAnnouncements": ["second", "first"]}
    ownership.apply_source(selected, [native_plan(path, native)])
    assert get(path) == native
    ownership.apply_source(selected, [native_plan(path, native)])
    put(path, {"companyAnnouncements": ["second", "first", "personal"]})
    with pytest.raises(RuntimeError, match="Unowned array elements"):
        ownership.apply_source(selected, [native_plan(path, native)])
    assert get(path)["companyAnnouncements"] == ["second", "first", "personal"]
    ownership.release_owner("integration", "claude")
    assert get(path) == {"companyAnnouncements": ["personal"]}
    put(path, {"companyAnnouncements": ["first", "second"]})
    with pytest.raises(RuntimeError, match="Unowned array elements"):
        ownership.apply_source(selected, [native_plan(path, native)])


@pytest.mark.parametrize("replacement", [[], ["new"]])
def test_exact_array_retirement_allows_replacement_and_owned_empty_leaf(destinations, replacement):
    path = destinations["claude"]
    put(path, {"companyAnnouncements": ["legacy"], "personal": True})
    selected = source(handoff=handoff("retire", ["companyAnnouncements"], elements=["legacy"]))
    native = {"companyAnnouncements": replacement}
    ownership.apply_source(selected, [native_plan(path, native)])
    assert get(path) == {"companyAnnouncements": replacement, "personal": True}
    entry = files._find_entry(files._load_manifest(), "claude", path)
    assert entry["active_effects"] == [
        {"path": ["companyAnnouncements"], "value": replacement, "elements": replacement}
    ]
    ownership.apply_source(selected, [native_plan(path, native)])
    assert get(path)["companyAnnouncements"] == replacement
    ownership.release_owner("integration", "claude")
    assert get(path) == {"personal": True}


def test_exact_org_array_cannot_be_replaced_with_scalar_without_retirement(destinations):
    path = destinations["claude"]
    put(path, {"forceLoginOrgUUID": ["old"]})
    native = {"forceLoginOrgUUID": "new"}
    with pytest.raises(RuntimeError, match="Unowned array elements"):
        ownership.apply_source(source(), [native_plan(path, native)])
    selected = source(handoff=handoff("retire", ["forceLoginOrgUUID"], elements=["old"]))
    ownership.apply_source(selected, [native_plan(path, native)])
    assert get(path) == native


def test_owned_native_exporter_can_switch_variant_after_cleanup(destinations):
    path = destinations["claude"]
    first = {"otel": {"exporter": {"otlp-grpc": {"endpoint": "first"}}}}
    second = {"otel": {"exporter": {"otlp-http": {"endpoint": "second", "protocol": "json"}}}}
    put(path, {"otel": {"metrics_exporter": "statsig"}})
    for native in (first, second, {"otel": {"exporter": "none"}}):
        ownership.apply_source(source(), [native_plan(path, native, tool="codex")])
        assert get(path) == {"otel": {"metrics_exporter": "statsig", **native["otel"]}}
    ownership.apply_source(source(), [native_plan(path, {}, tool="codex")])
    assert get(path) == {"otel": {"metrics_exporter": "statsig"}}


def test_adoption_requires_current_effect_and_arrays_explicit_contributions(destinations):
    path = destinations["claude"]
    put(path, {"allow": ["owned", "personal"]})
    selected = source(handoff=handoff("adopt", ["allow"], elements=["owned"]))
    ownership.apply_source(selected, [plan(path, {"allow": ["owned"]})])
    ownership.apply_source(selected, [plan(path, {"allow": ["owned"]})])
    ownership.apply_source(source(), [plan(path, {})])
    assert get(path) == {"allow": ["personal"]}
    missing = handoff("adopt", ["missing"])
    missing["migration_version"] = 2
    with pytest.raises(RuntimeError, match="current declaration"):
        ownership.apply_source(source(handoff=missing), [plan(path, {})])


def test_retirement_receipt_cannot_redelete_later_user_value(destinations):
    path = destinations["claude"]
    put(path, {"old": "legacy", "personal": 1})
    selected = source(handoff=handoff("retire", ["old"]))
    ownership.apply_source(selected, [plan(path, {"new": 2})])
    put(path, {**get(path), "old": "later-user"})
    ownership.apply_source(selected, [plan(path, {"new": 2})])
    assert get(path)["old"] == "later-user"
    ownership.release_owner("integration", "claude")
    ownership.apply_source(selected, [plan(path, {"new": 2})])
    assert get(path)["old"] == "later-user"


@pytest.mark.parametrize("change", ["different", "removed"])
def test_changed_migration_declarations_require_new_version(destinations, change):
    path = destinations["claude"]
    ownership.apply_source(source(handoff=handoff("retire", ["old"])), [plan(path, {})])
    changed = handoff("retire", ["different"])
    if change == "removed":
        changed["agents"] = {}
    with pytest.raises(RuntimeError, match="increment migration_version"):
        ownership.apply_source(source(handoff=changed), [plan(path, {})])


def test_preflight_parses_all_destinations_before_writing(destinations):
    first, second = destinations["claude"], destinations["user"]
    put(first, {"personal": 1})
    second.write_text("invalid JSON")
    with pytest.raises(RuntimeError, match="Cannot parse"):
        ownership.apply_source(
            source(), [plan(first, {"a": 1}), plan(second, {"b": 2}, "user_settings")]
        )
    assert get(first) == {"personal": 1}
    assert ownership.applied_source("claude") is None


@pytest.mark.parametrize("fail_at", [1, 2])
def test_partial_failure_preserves_journal_and_retry_keeps_user_edits(
    destinations, monkeypatch, fail_at
):
    first, second = destinations["claude"], destinations["user"]
    original = ownership._atomic_replace
    count = 0

    def fail(path, text, expected):
        nonlocal count
        count += 1
        if count == fail_at:
            raise OSError("injected")
        original(path, text, expected)

    monkeypatch.setattr(ownership, "_atomic_replace", fail)
    plans = [plan(first, {"a": 1}), plan(second, {"b": 2}, "user_settings")]
    with pytest.raises(OSError, match="injected"):
        ownership.apply_source(source(), plans)
    assert ownership.applied_source("claude")["status"] == "pending"
    assert not files._load_manifest().get("applications")
    put(first, {**get(first), "personal": 4})
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    ownership.apply_source(source(), plans)
    assert get(first) == {"a": 1, "personal": 4}
    assert get(second) == {"b": 2}
    assert ownership.applied_source("claude")["status"] == "applied"


def test_two_failed_source_switches_retain_all_pending_ownership(destinations, monkeypatch):
    first, second = destinations["claude"], destinations["user"]

    def plans(name):
        return [plan(first, {name: 1}), plan(second, {name: 1}, "user_settings")]

    ownership.apply_source(source(), plans("a"))
    original = ownership._atomic_replace

    def fail_second(path, text, expected):
        if path == second:
            raise OSError("second")
        original(path, text, expected)

    monkeypatch.setattr(ownership, "_atomic_replace", fail_second)
    with pytest.raises(OSError):
        ownership.apply_source(source(owner="b"), plans("b"))
    monkeypatch.setattr(ownership, "_atomic_replace", Mock(side_effect=OSError("first")))
    with pytest.raises(OSError):
        ownership.apply_source(source(owner="c"), plans("c"))
    put(first, {**get(first), "personal": True})
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    ownership.apply_source(source(owner="c"), plans("c"))
    assert get(first) == {"c": 1, "personal": True}
    assert get(second) == {"c": 1}


def test_external_edit_after_planning_is_preserved(destinations):
    path = destinations["claude"]
    put(path, {"personal": 1})
    desired = plan(path, {"managed": 1})

    def compose(base):
        put(path, {"personal": "concurrent"})
        return {**base, "managed": 1}

    desired.compose = compose
    with pytest.raises(RuntimeError, match="Concurrent edit"):
        ownership.apply_source(source(), [desired])
    assert get(path) == {"personal": "concurrent"}
    assert ownership.applied_source("claude")["status"] == "pending"


def test_final_verification_detects_edit_to_earlier_destination(destinations, monkeypatch):
    first, second = destinations["claude"], destinations["user"]
    original = ownership._atomic_replace

    def external_edit(path, text, expected):
        original(path, text, expected)
        if path == second:
            put(first, {"concurrent": 3})

    monkeypatch.setattr(ownership, "_atomic_replace", external_edit)
    with pytest.raises(RuntimeError, match="remains incomplete"):
        ownership.apply_source(
            source(), [plan(first, {"a": 1}), plan(second, {"b": 2}, "user_settings")]
        )
    assert get(first) == {"concurrent": 3}
    assert ownership.applied_source("claude")["status"] == "pending"


def test_equal_managed_destination_records_ownership_without_sudo(destinations, monkeypatch):
    path = destinations["managed"]
    path.parent.mkdir(parents=True)
    path.write_text('{"personal": 1, "owned": 2}\n')
    sudo = Mock(side_effect=AssertionError("must not elevate"))
    monkeypatch.setattr(files, "_sudo_replace", sudo)
    ownership.apply_source(
        source(), [plan(path, {"owned": 2}, "managed_settings", privileged=True)]
    )
    sudo.assert_not_called()
    assert path.read_text() == '{"personal": 1, "owned": 2}\n'
    assert files._find_entry(files._load_manifest(), "claude", path)["active_effects"]


def test_retired_environment_survives_two_omissions_and_release_is_owner_scoped(destinations):
    path = destinations["claude"]
    ownership.apply_source(source(owner="a"), [plan(path, {})], process_env_keys={"OLD"})
    for _ in range(2):
        ownership.apply_source(source(owner="a"), [plan(path, {})])
        assert ownership.retired_environment("claude") == {"OLD"}
    ownership.apply_source(source(owner="b"), [plan(path, {})], process_env_keys={"NEW"})
    ownership.release_owner("a", "claude")
    assert ownership.retired_environment("claude") == set()
    assert files._load_manifest()["process_env"]["claude"] == {"NEW": "b"}
    ownership.apply_source(source(owner="b"), [plan(path, {})], process_env_keys={"OLD"})
    assert ownership.retired_environment("claude") == {"NEW"}


def process_handoff(action, name):
    declaration = handoff(action, [name])
    declaration["agents"]["claude"][action][0]["target"] = "process_env"
    return declaration


def test_process_adoption_requires_current_declaration_before_writes(destinations):
    path = destinations["claude"]
    selected = source(handoff=process_handoff("adopt", "ADOPTED"))
    with pytest.raises(RuntimeError, match="current custom_env declaration"):
        ownership.apply_source(selected, [plan(path, {"generated": True})])
    assert not path.exists()
    assert ownership.applied_source("claude") is None
    ownership.apply_source(selected, [plan(path, {})], process_env_keys={"ADOPTED"})
    assert files._load_manifest()["process_env"]["claude"] == {"ADOPTED": "integration"}
    ownership.apply_source(selected, [plan(path, {})])
    assert ownership.retired_environment("claude") == {"ADOPTED"}


def test_process_retirement_is_durable_one_time_and_transfer_safe(destinations):
    from ucode.child_env import build_child_env

    selected = source(handoff=process_handoff("retire", "OLD"))
    path = destinations["claude"]
    for _ in range(2):
        ownership.apply_source(selected, [plan(path, {})])
        assert build_child_env({}, "claude", inherited={"OLD": "parent-stale", "KEEP": "user"}) == {
            "KEEP": "user"
        }
    manifest = files._load_manifest()
    assert "integration:1:claude:process_env" in manifest["migration_receipts"]
    assert ownership.release_owner("integration", "claude")
    ownership.apply_source(selected, [plan(path, {})])
    assert ownership.retired_environment("claude") == set()
    assert build_child_env({}, "claude", inherited={"OLD": "new-user"}) == {"OLD": "new-user"}
    ownership.apply_source(source(owner="next"), [plan(path, {})], process_env_keys={"OLD"})
    ownership.release_owner("integration", "claude")
    assert files._load_manifest()["process_env"]["claude"] == {"OLD": "next"}
    ownership.apply_source(source(owner="next", workspace="https://b.example"), [plan(path, {})])
    assert ownership.retired_environment("claude") == {"OLD"}
    ownership.release_owner("integration", "claude")
    assert ownership.retired_environment("claude") == {"OLD"}


def test_process_receipt_is_not_committed_when_destination_write_fails(destinations, monkeypatch):
    path = destinations["claude"]
    selected = source(handoff=process_handoff("retire", "OLD"))
    original = ownership._atomic_replace
    monkeypatch.setattr(ownership, "_atomic_replace", Mock(side_effect=OSError("test failure")))
    with pytest.raises(OSError, match="test failure"):
        ownership.apply_source(selected, [plan(path, {"generated": True})])
    assert "integration:1:claude:process_env" not in files._load_manifest()["migration_receipts"]
    assert ownership.retired_environment("claude") == set()
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    ownership.apply_source(selected, [plan(path, {"generated": True})])
    assert ownership.retired_environment("claude") == {"OLD"}


def test_process_handoff_changed_receipt_rejected(destinations):
    path = destinations["claude"]
    ownership.apply_source(source(handoff=process_handoff("retire", "OLD")), [plan(path, {})])
    with pytest.raises(RuntimeError, match="increment migration_version"):
        ownership.apply_source(
            source(handoff=process_handoff("retire", "NEW")), [plan(path, {"new": 1})]
        )
    assert get(path) == {}


@pytest.mark.parametrize("final_source", ["file", "api"])
def test_failed_applications_retain_process_ownership_across_source_changes(
    destinations, monkeypatch, final_source
):
    from ucode.child_env import build_child_env

    private, managed = destinations["claude"], destinations["managed"]
    original = ownership._atomic_replace

    def fail_managed(path, text, previous):
        if path == managed:
            raise OSError("blocked managed write")
        original(path, text, previous)

    monkeypatch.setattr(ownership, "_atomic_replace", fail_managed)
    for owner, name in [("first", "OLD"), ("second", "NEXT")]:
        selected = source(owner=owner)
        with pytest.raises(OSError, match="blocked managed write"):
            ownership.apply_source(
                selected,
                [
                    plan(private, {"env": {name: owner}}),
                    plan(managed, {"env": {name: owner}}, "managed_settings"),
                ],
                process_env_keys={name},
            )
        assert get(private) == {"env": {name: owner}}
    assert files._load_manifest()["pending"]["claude"]["process_env"] == {
        "OLD": "first",
        "NEXT": "second",
    }
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    selected = (
        source(owner="third")
        if final_source == "file"
        else SelectedManagedSource.from_api((None, False), "https://b.example", "claude")
    )
    ownership.apply_source(selected, [plan(private, {}), plan(managed, {}, "managed_settings")])
    assert get(private) == {}
    assert ownership.retired_environment("claude") == {"OLD", "NEXT"}
    for _ in range(2):
        assert build_child_env(
            {}, "claude", inherited={"OLD": "stale", "NEXT": "stale", "KEEP": "user"}
        ) == {"KEEP": "user"}
    ownership.release_owner("first", "claude")
    assert ownership.retired_environment("claude") == {"NEXT"}


@pytest.mark.parametrize(
    "item",
    [
        {"target": "process_env", "path": ["ONE", "TWO"]},
        {"target": "process_env", "path": ["ONE"], "elements": []},
        {"target": "process_env", "path": ["OAUTH_TOKEN"]},
        {"target": "process_env", "path": ["INVALID-NAME"]},
    ],
)
def test_invalid_process_handoff_is_rejected_by_parser(item):
    from tests.test_managed_source import wire

    raw = wire("claude")
    raw["handoff"] = process_handoff("retire", "OLD")
    raw["handoff"]["agents"]["claude"]["retire"] = [item]
    with pytest.raises(RuntimeError):
        validate_file_config(raw, "claude")


def test_windows_process_names_share_one_ownership_identity(destinations, monkeypatch):
    from types import SimpleNamespace

    from tests.test_managed_source import wire
    from ucode import child_env

    monkeypatch.setattr(child_env, "os", SimpleNamespace(name="nt", environ={}))
    path = destinations["claude"]
    ownership.apply_source(source(), [plan(path, {})], process_env_keys={"Review_Key"})
    ownership.apply_source(source(), [plan(path, {})], process_env_keys={"REVIEW_KEY"})
    assert ownership.retired_environment("claude") == set()
    ownership.apply_source(source(), [plan(path, {})])
    assert child_env.build_child_env({}, "claude", inherited={"review_key": "stale"}) == {}
    raw = wire("claude")
    raw["handoff"] = process_handoff("adopt", "Review_Key")
    raw["handoff"]["agents"]["claude"]["retire"] = [
        {"target": "process_env", "path": ["REVIEW_KEY"]}
    ]
    with pytest.raises(RuntimeError, match="overlapping handoff declarations"):
        validate_file_config(raw, "claude")


def test_release_cli_does_not_discover_or_launch(destinations, monkeypatch):
    path = destinations["claude"]
    put(path, {"personal": 1})
    ownership.apply_source(source(owner="integration"), [plan(path, {"generated": 2})])
    monkeypatch.setattr(
        cli, "ensure_bootstrap_dependencies", Mock(side_effect=AssertionError("bootstrap"))
    )
    monkeypatch.setattr(cli, "launch_agent", Mock(side_effect=AssertionError("launch")))
    monkeypatch.setattr(cli, "get_managed_config", Mock(side_effect=AssertionError("discovery")))
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(
            cli.app, ["managed-config", "release", "--owner", "integration", "--agent", "claude"]
        )
        assert result.exit_code == 0, result.output
    assert get(path) == {"personal": 1}


def test_release_rejects_tampered_recorded_destination(destinations, tmp_path):
    ownership.apply_source(source(), [plan(destinations["claude"], {"owned": 1})])
    manifest = files._load_manifest()
    entry = next(iter(manifest["files"].values()))
    outside = tmp_path / "unregistered.json"
    put(outside, {"owned": "must-survive"})
    entry["path"] = str(outside)
    files._write_manifest(manifest)
    with pytest.raises(RuntimeError, match="Unrecognized"):
        ownership.release_owner("local-file", "claude")
    assert get(outside) == {"owned": "must-survive"}


def test_ordinary_cleanup_does_not_restore_retired_baseline_but_revert_does(destinations):
    path = destinations["claude"]
    put(path, {"owned": "legacy", "personal": 1})
    ownership.apply_source(source(), [plan(path, {"owned": "new"})])
    ownership.apply_source(source(), [plan(path, {})])
    assert get(path) == {"personal": 1}
    assert ownership.revert_owned_destinations("claude")
    assert get(path) == {"owned": "legacy", "personal": 1}


def test_v1_migration_preserves_original_snapshot_and_supports_second_destination(destinations):
    first, second = destinations["managed"], destinations["user"]
    original, last = '{"original": 1}\n', '{"original": 1, "old": 2}\n'
    files._write_private_file(
        files.MANAGED_BACKUP_DIR / "claude-managed-settings.backup.json", original
    )
    files._write_private_file(
        files.MANAGED_BACKUP_DIR / "claude-managed-settings.last-applied.json", last
    )
    files._write_manifest(
        {
            "version": 1,
            "files": {
                "claude": {
                    "path": str(first),
                    "original_existed": True,
                    "backup_file": "claude-managed-settings.backup.json",
                    "original_sha256": files._sha256(original),
                    "last_applied_file": "claude-managed-settings.last-applied.json",
                    "last_applied_sha256": files._sha256(last),
                    "owned_paths": [["old"]],
                }
            },
        }
    )
    first.parent.mkdir(parents=True)
    first.write_text(last)
    ownership.apply_source(
        source(),
        [plan(first, {"new": 3}, "managed_settings"), plan(second, {"x": 4}, "user_settings")],
    )
    entry = files._find_entry(files._load_manifest(), "claude", first)
    assert files._original_text(entry) == original
    assert entry["original_sha256"] == files._sha256(original)
    assert entry["backup_file"] == "claude-managed-settings.backup.json"
    assert get(first) == {"original": 1, "new": 3}
    assert len(files._load_manifest()["files"]) == 2


def test_newly_disabled_agent_blocks_transition_with_release_guidance(destinations):
    ownership.apply_source(source("codex"), [plan(destinations["codex"], {"managed": 1})])
    selected = replace(source(), _manifest_json=json.dumps({"enabled_agents": {"claude": {}}}))
    with pytest.raises(RuntimeError, match="release --owner local-file --agent codex"):
        ownership.preflight_source_transition(selected)


def test_initial_pending_disabled_agent_blocks_other_agent_before_write(destinations, monkeypatch):
    original = ownership._atomic_replace
    monkeypatch.setattr(ownership, "_atomic_replace", Mock(side_effect=OSError("failed")))
    with pytest.raises(OSError):
        ownership.apply_source(source(), [plan(destinations["claude"], {"a": 1})])
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    selected = replace(
        source("codex"), _manifest_json=json.dumps({"enabled_agents": {"codex": {}}})
    )
    with pytest.raises(RuntimeError, match="release --owner local-file --agent claude"):
        ownership.apply_source(selected, [plan(destinations["codex"], {"b": 1})])
    assert not destinations["codex"].exists()


def test_partial_release_retries_without_restoring_baseline(destinations, monkeypatch):
    first, second = destinations["claude"], destinations["user"]
    put(first, {"owned": "before", "personal": 1})
    ownership.apply_source(
        source(owner="integration"),
        [plan(first, {"owned": "managed"}), plan(second, {"generated": True}, "user_settings")],
    )
    original = ownership._atomic_replace

    def fail_second(path, text, expected):
        if path == second:
            raise OSError("release failed")
        original(path, text, expected)

    monkeypatch.setattr(ownership, "_atomic_replace", fail_second)
    with pytest.raises(OSError):
        ownership.release_owner("integration", "claude")
    assert ownership.applied_source("claude")["status"] == "pending"
    put(first, {**get(first), "later": 2})
    monkeypatch.setattr(ownership, "_atomic_replace", original)
    ownership.release_owner("integration", "claude")
    assert get(first) == {"personal": 1, "later": 2}
    assert get(second) == {}
    assert ownership.applied_source("claude")["status"] == "released"


def test_revert_preserves_unowned_edit_incorporated_by_later_application(destinations):
    path = destinations["claude"]
    put(path, {"owned": "before", "personal": 1})
    ownership.apply_source(source(), [plan(path, {"owned": "first"})])
    put(path, {"owned": "first", "personal": 2, "new_personal": 3})
    ownership.apply_source(source(), [plan(path, {"owned": "second"})])
    assert ownership.revert_owned_destinations("claude")
    assert get(path) == {"owned": "before", "personal": 2, "new_personal": 3}


def test_status_and_doctor_report_applied_file_then_pending_and_released(destinations, monkeypatch):
    from ucode.doctor import _check_applied_sources

    path = destinations["claude"]
    selected = source(owner="integration")
    ownership.apply_source(selected, [plan(path, {"owned": 1})])
    local_state = {"workspace": selected.workspace, "available_tools": ["claude"]}
    monkeypatch.setattr(cli, "load_state", lambda: local_state)
    monkeypatch.setattr(cli, "load_managed_state", lambda _workspace: None)
    monkeypatch.setattr(cli, "_live_status_managed_state", lambda *_args: (None, "live"))
    monkeypatch.setattr(cli, "_live_status_model_state", lambda state, _tools: (state, "cached"))
    monkeypatch.setattr(cli, "configured_skill_counts_by_agent", lambda *_args: {})
    monkeypatch.setattr(cli, "managed_mcp_server_names", lambda *_args: set())
    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "File-managed (applied)" in result.output
    assert "/input/a.json" in result.output
    assert "integration" in result.output
    assert _check_applied_sources()[0].status == "ok"
    assert "/input/a.json" in _check_applied_sources()[0].detail
    writer = ownership._atomic_replace
    monkeypatch.setattr(ownership, "_atomic_replace", Mock(side_effect=OSError("failed")))
    with pytest.raises(OSError):
        ownership.apply_source(
            replace(selected, resolved_path=Path("/input/b.json")), [plan(path, {"owned": 2})]
        )
    assert _check_applied_sources()[0].status == "error"
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "File-managed (incomplete)" in result.output
    assert "/input/b.json" in result.output
    monkeypatch.setattr(ownership, "_atomic_replace", writer)
    ownership.release_owner("integration", "claude")
    assert _check_applied_sources() == []


def test_partial_application_does_not_overwrite_successful_snapshot(destinations, monkeypatch):
    path, second = destinations["claude"], destinations["user"]
    ownership.apply_source(source(), [plan(path, {"a": 1})])
    original_entry = deepcopy(files._find_entry(files._load_manifest(), "claude", path))
    original_snapshot = files._snapshot_text(original_entry, "last_applied_file")
    writer = ownership._atomic_replace

    def fail_second(target, text, expected):
        if target == second:
            raise OSError("failed")
        writer(target, text, expected)

    monkeypatch.setattr(ownership, "_atomic_replace", fail_second)
    with pytest.raises(OSError):
        ownership.apply_source(
            source(), [plan(path, {"b": 1}), plan(second, {"b": 1}, "user_settings")]
        )
    assert files._snapshot_text(original_entry, "last_applied_file") == original_snapshot


@pytest.mark.parametrize("cleanup", ["retry", "release"])
@pytest.mark.parametrize("prior_application", [False, True])
@pytest.mark.parametrize("failed_omission", [False, True])
def test_partial_application_history_survives_omission_and_broad_revert(
    destinations, monkeypatch, cleanup, prior_application, failed_omission
):
    first, second = destinations["claude"], destinations["user"]
    put(first, {"x": "user", "unrelated": "original"})
    if prior_application:
        ownership.apply_source(source(), [plan(first, {"previous": True})])
    writer = ownership._atomic_replace

    def fail_second(path, text, expected):
        if path == second:
            raise OSError("second destination failed")
        writer(path, text, expected)

    monkeypatch.setattr(ownership, "_atomic_replace", fail_second)
    with pytest.raises(OSError, match="second destination failed"):
        ownership.apply_source(
            source(), [plan(first, {"x": "managed"}), plan(second, {"y": 1}, "user_settings")]
        )
    assert get(first)["x"] == "managed"
    put(first, {**get(first), "unrelated": "newer"})
    if failed_omission:
        with pytest.raises(OSError, match="second destination failed"):
            ownership.apply_source(
                source(), [plan(first, {}), plan(second, {"y": 2}, "user_settings")]
            )
        assert get(first) == {"unrelated": "newer"}
    monkeypatch.setattr(ownership, "_atomic_replace", writer)

    if cleanup == "release":
        ownership.release_owner("local-file", "claude")
    else:
        ownership.apply_source(source(), [plan(first, {}), plan(second, {}, "user_settings")])
    assert get(first) == {"unrelated": "newer"}
    entry = files._find_entry(files._load_manifest(), "claude", first)
    assert ["x"] in entry["owned_paths"]
    assert json.loads(files._original_text(entry)) == {"x": "user", "unrelated": "original"}

    ownership.revert_owned_destinations("claude")
    assert get(first) == {"x": "user", "unrelated": "newer"}


def test_generated_catalog_replaces_previous_contents_then_release_removes_it(
    destinations, monkeypatch, tmp_path
):
    path = tmp_path / "catalog.json"
    monkeypatch.setattr(codex, "CODEX_MODEL_CATALOG_PATH", path)
    put(path, {"models": [{"slug": "old"}]})
    ownership.apply_source(
        source("codex"),
        [
            plan(
                path,
                {"models": [{"slug": "new"}]},
                "model_catalog",
                generated_artifact=True,
                delete_when_empty=True,
                baseline_supplied=True,
            )
        ],
    )
    assert get(path) == {"models": [{"slug": "new"}]}
    ownership.release_owner("local-file", "codex")
    assert not path.exists()


def test_explicit_api_configure_transitions_active_and_released_file_source(
    destinations, monkeypatch
):
    from ucode import managed_config

    path = destinations["claude"]
    ownership.apply_source(source(), [plan(path, {"owned": 1})])
    fetch = Mock(return_value=managed_config.ManagedConfigResult(None, False))
    monkeypatch.setattr(managed_config, "refresh_managed_config", fetch)
    selected = ownership.source_for_writer({"workspace": "https://b.example"}, "claude", None)
    assert selected.kind == "api"
    assert selected.manifest is None
    ownership.apply_source(selected, [plan(path, {"generated": 2})])
    assert get(path) == {"generated": 2}
    ownership.release_owner("workspace-api", "claude")
    fetch.reset_mock()
    selected = ownership.source_for_writer({"workspace": "https://b.example"}, "claude", None)
    assert selected.kind == "api"
    fetch.assert_called_once()
    ownership.apply_source(selected, [plan(path, {"api_header": "old"})])
    ownership.apply_source(source(), [plan(path, {"file_header": "new"})])
    assert get(path) == {"file_header": "new"}


@pytest.mark.parametrize("target", ["/tmp/arbitrary", "model_catalog"])
def test_handoff_rejects_unavailable_or_internal_targets(target):
    raw = {
        "spec_version": 1,
        "enabled_agents": [
            {"agent": "claude", "config": {"default_models": {"default_model": "system.ai.model"}}}
        ],
        "handoff": handoff("retire", ["x"]),
    }
    raw["handoff"]["agents"]["claude"]["retire"][0]["target"] = target
    with pytest.raises(RuntimeError, match="handoff target"):
        validate_file_config(raw, "claude")


def _concurrent_application(directory, path, agent, gate):
    files.MANAGED_BACKUP_DIR = Path(directory)
    files.MANAGED_BACKUP_MANIFEST_PATH = Path(directory) / "manifest.json"
    gate.wait(10)
    desired = plan(Path(path), {agent: True})
    compose = desired.compose

    def slow(base):
        time.sleep(0.1)
        return compose(base)

    desired.compose = slow
    ownership.apply_source(source(agent), [desired])


def test_real_processes_serialize_shared_destination_without_lost_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    path = tmp_path / "shared.json"
    workers = [
        context.Process(
            target=_concurrent_application,
            args=(str(files.MANAGED_BACKUP_DIR), str(path), agent, gate),
        )
        for agent in ("claude", "codex")
    ]
    for worker in workers:
        worker.start()
    gate.set()
    try:
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(5)
    assert get(path) == {"claude": True, "codex": True}
    assert set(files._load_manifest()["applications"]) == {"claude", "codex"}
