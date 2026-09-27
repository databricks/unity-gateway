"""Managed-file revert after incomplete ownership transactions, on isolated files."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from ucode import managed_files as files
from ucode import managed_ownership as ownership
from ucode.agents import claude
from ucode.config_io import deep_merge_dict
from ucode.managed_source import SelectedManagedSource


@pytest.fixture
def destinations(tmp_path, monkeypatch):
    managed = tmp_path / "etc" / "managed-settings.json"
    private = tmp_path / "claude" / "settings.json"
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", private)
    monkeypatch.setattr(files, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(files, "_SUDO_REPLACE_TARGETS", {files.current_os(): frozenset({managed})})
    atomic_replace = ownership._atomic_replace

    def replace(path, text, *, expected_text=files._MISSING):
        assert path == managed
        if expected_text is files._MISSING:
            expected_text = files.read_managed_file(path)
        atomic_replace(path, text, expected_text)

    def remove(path):
        assert path == managed
        atomic_replace(path, None, files.read_managed_file(path))

    monkeypatch.setattr(files, "_sudo_replace", replace)
    monkeypatch.setattr(files, "_sudo_remove", remove)
    return managed, private


def source():
    return SelectedManagedSource(
        kind="file",
        workspace="https://example.databricks.com",
        agent="claude",
        resolved_path=Path("/input/policy.json"),
        digest="test-input",
        _manifest_json='{"enabled_agents":{"claude":{}}}',
    )


def plan(path, overlay, *, managed=False):
    return ownership.DestinationPlan(
        target="managed_settings" if managed else "private_settings",
        path=path,
        parser=json.loads,
        dumper=lambda doc: json.dumps(doc) + "\n",
        compose=lambda base: deep_merge_dict(base, deepcopy(overlay)),
        owned_paths=ownership.leaf_paths(overlay),
        privileged=managed,
    )


def apply_partial(destinations, monkeypatch, overlay):
    managed, private = destinations
    atomic_replace = ownership._atomic_replace

    def fail_later(path, text, expected):
        if path == private:
            raise OSError("injected later destination failure")
        atomic_replace(path, text, expected)

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_atomic_replace", fail_later)
        with pytest.raises(OSError, match="later destination"):
            ownership.apply_source(
                source(), [plan(managed, overlay, managed=True), plan(private, {"private": True})]
            )


def revert(path):
    return files.revert_managed_file(
        "claude",
        display="Claude Code",
        parser=json.loads,
        dumper=lambda doc: json.dumps(doc) + "\n",
        path=path,
    )


def snapshot_bytes():
    return {
        path.name: path.read_bytes()
        for path in files.MANAGED_BACKUP_DIR.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("drift", ["none", "unrelated", "owned"])
def test_revert_uses_verified_pending_snapshot_and_preserves_drift(
    destinations, monkeypatch, existing, drift
):
    managed, _ = destinations
    baseline = {"enterprise": True, "rules": ["original"]} if existing else {}
    if existing:
        managed.parent.mkdir(parents=True)
        managed.write_text(json.dumps(baseline) + "\n")
    apply_partial(destinations, monkeypatch, {"policy": "managed", "rules": ["owned"]})
    manifest = files._load_manifest()
    key = files._destination_key("claude", managed)
    assert "last_applied_file" not in manifest["files"][key]
    assert manifest["pending"]["claude"]["entries"][key]["last_applied_file"]
    expected = deepcopy(baseline)
    if drift != "none":
        current = json.loads(managed.read_text())
        current["personal"] = "later"
        current["rules"].append("later")
        expected.update(personal="later", rules=[*baseline.get("rules", []), "later"])
        if drift == "owned":
            current["policy"] = "user-edit"
            expected["policy"] = "user-edit"
        managed.write_text(json.dumps(current))

    revert(managed)

    assert (json.loads(managed.read_text()) if managed.exists() else {}) == expected
    manifest = files._load_manifest()
    assert key not in manifest["files"]
    pending = manifest["pending"]["claude"]
    assert pending["entries"]
    for section in ("entries", "recovery_entries", "before"):
        assert key not in pending.get(section, {})
    assert key not in pending["attempted"]
    ownership.revert_owned_destinations("claude")
    assert not files._load_manifest().get("pending")
    assert (json.loads(managed.read_text()) if managed.exists() else {}) == expected
    assert revert(managed) == "unchanged"


def test_revert_keeps_initial_baseline_across_two_incomplete_applications(
    destinations, monkeypatch
):
    managed, private = destinations
    managed.parent.mkdir(parents=True)
    managed.write_text('{"enterprise": "original"}\n')
    ownership.apply_source(source(), [plan(managed, {"first": True}, managed=True)])
    original_entry = deepcopy(files._find_entry(files._load_manifest(), "claude", managed))
    apply_partial(destinations, monkeypatch, {"second": True})
    with monkeypatch.context() as patch:
        patch.setattr(
            files, "_sudo_replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied"))
        )
        with pytest.raises(RuntimeError, match="Could not apply"):
            ownership.apply_source(
                source(),
                [plan(managed, {"third": True}, managed=True), plan(private, {"private": True})],
            )
    assert files._original_text(original_entry) == '{"enterprise": "original"}\n'
    assert revert(managed) == "restored"
    assert json.loads(managed.read_text()) == {"enterprise": "original"}
    ownership.revert_owned_destinations("claude")
    assert not files._load_manifest().get("pending")


@pytest.mark.parametrize("prior_apply", [False, True])
@pytest.mark.parametrize("failure", ["write_then_raise", "unverified_edit"])
def test_revert_retains_recovery_for_unverified_attempts(
    destinations, monkeypatch, prior_apply, failure
):
    managed, _ = destinations
    if prior_apply:
        ownership.apply_source(source(), [plan(managed, {"first": True}, managed=True)])
    replace = files._sudo_replace

    def unverified_write(path, text, **kwargs):
        replace(path, text, **kwargs)
        if failure == "write_then_raise":
            raise PermissionError("write completed before failure")
        path.write_text(json.dumps({**json.loads(text), "concurrent": True}))

    with monkeypatch.context() as patch:
        patch.setattr(files, "_sudo_replace", unverified_write)
        with pytest.raises(RuntimeError, match="recovery journal retained"):
            ownership.apply_source(source(), [plan(managed, {"second": True}, managed=True)])
    before = snapshot_bytes()
    current = managed.read_bytes()
    with pytest.raises(RuntimeError, match="no verified snapshot.*retained"):
        revert(managed)
    assert managed.read_bytes() == current
    assert snapshot_bytes() == before
    assert files._load_manifest()["pending"]["claude"]


@pytest.mark.parametrize("failure", ["permission", "verification"])
def test_failed_revert_retains_snapshots_and_pending_destination(
    destinations, monkeypatch, failure
):
    managed, _ = destinations
    managed.parent.mkdir(parents=True)
    managed.write_text('{"enterprise": true}\n')
    apply_partial(destinations, monkeypatch, {"policy": "managed"})
    before = snapshot_bytes()
    current = managed.read_bytes()

    def failed_restore(*args, **kwargs):
        if failure == "permission":
            raise PermissionError("denied")

    with monkeypatch.context() as patch:
        patch.setattr(files, "_sudo_replace", failed_restore)
        with pytest.raises(RuntimeError, match="backup was retained"):
            revert(managed)
    assert managed.read_bytes() == current
    assert snapshot_bytes() == before
    assert revert(managed) == "restored"
    assert json.loads(managed.read_text()) == {"enterprise": True}
