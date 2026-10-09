"""Filesystem contract tests for Claude Desktop profile configuration."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ucode.agents import claude_desktop as desktop


def _uuid() -> str:
    return str(uuid.uuid4())


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _seed_metadata(directory: Path, *, name: str = "Default") -> str:
    profile_id = _uuid()
    _write_json(
        directory / desktop.META_FILENAME,
        {"appliedId": profile_id, "entries": [{"id": profile_id, "name": name}]},
    )
    _write_json(directory / f"{profile_id}.json", {"inferenceProvider": "firstParty"})
    return profile_id


def _configure(
    directory: Path,
    state_path: Path,
    *,
    workspace: str = "https://workspace.example",
    models: tuple[str, ...] = ("databricks/anthropic/claude-sonnet-4", "openai/gpt-oss"),
) -> desktop.ConfigureResult:
    return desktop.configure_claude_desktop(
        workspace,
        model_ids=models,
        base_url=f"{workspace}/ai-gateway/anthropic",
        helper_command="/Applications/Unity Gateway/bin/ug with spaces",
        helper_args=("claude-desktop-auth", "--host", "host", "--profile", "selectedprofile"),
        directory=directory,
        state_path=state_path,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_render_profile_uses_exact_owned_fields_and_full_model_ids(tmp_path: Path) -> None:
    profile = desktop.render_profile(
        model_ids=("databricks/anthropic/claude-sonnet-4", "openai/gpt-oss-120b"),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command=tmp_path / "Program Files" / "Unity Gateway" / "ug",
        helper_args=("claude-desktop-auth", "--host", "host", "--profile", "selectedprofile"),
    )

    assert profile == {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": "https://workspace.example/ai-gateway/anthropic",
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": str(tmp_path / "Program Files" / "Unity Gateway" / "ug"),
        "inferenceCredentialHelperArgs": [
            "claude-desktop-auth",
            "--host",
            "host",
            "--profile",
            "selectedprofile",
        ],
        "inferenceCredentialHelperTtlSec": 900,
        "inferenceCredentialHelperTimeoutSec": 300,
        "inferenceCredentialHelperSilentRefreshEnabled": True,
        "modelDiscoveryEnabled": False,
        "inferenceModels": [
            {"name": "databricks/anthropic/claude-sonnet-4"},
            {"name": "openai/gpt-oss-120b"},
        ],
    }
    assert "defaultModel" not in profile
    assert "inferenceGatewayCustomHeaders" not in profile


def test_render_profile_accepts_observed_headers_and_default_boolean(tmp_path: Path) -> None:
    profile = desktop.render_profile(
        model_ids=("system.ai.claude-opus-5-5",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command=tmp_path / "ug",
        helper_args=("auth-helper",),
        custom_headers={"x-fake-header": "foo"},
        always_start_with_default_model=True,
    )

    assert profile["inferenceCustomHeaders"] == {"x-fake-header": "foo"}
    assert profile["alwaysStartWithDefaultModel"] is True
    assert profile["inferenceModels"] == [{"name": "system.ai.claude-opus-5-5"}]

    with pytest.raises(ValueError, match="CR/LF"):
        desktop.render_profile(
            model_ids=("model",),
            base_url="https://workspace.example/ai-gateway/anthropic",
            helper_command="/usr/local/bin/ug",
            helper_args=("auth-helper",),
            custom_headers={"x-header": "bad\nvalue"},
        )


def test_configure_creates_profile_and_metadata_for_fresh_injected_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "ucode" / "claude-desktop.json"

    result = _configure(directory, state_path)

    assert result.changed is True
    metadata = _read(directory / desktop.META_FILENAME)
    assert metadata["appliedId"] == result.profile_id
    assert [entry["name"] for entry in metadata["entries"]] == ["Unity Gateway"]
    profile = _read(result.profile_path)
    assert profile["inferenceModels"] == [
        {"name": "databricks/anthropic/claude-sonnet-4"},
        {"name": "openai/gpt-oss"},
    ]
    assert profile["inferenceCredentialHelper"] == "/Applications/Unity Gateway/bin/ug with spaces"
    assert "defaultModel" not in profile
    assert "inferenceGatewayCustomHeaders" not in profile
    assert _read(state_path)["profiles"]


def test_configure_preserves_existing_profiles_metadata_and_unrelated_profile_keys(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "Claude-3p"
    directory.mkdir()
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    existing_id = _uuid()
    existing_payload = {"userOwned": {"color": "blue"}, "inferenceProvider": "user"}
    _write_json(directory / f"{existing_id}.json", existing_payload)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": existing_id, "name": "Personal"})
    metadata["otherMetadata"] = {"keep": True}
    _write_json(directory / desktop.META_FILENAME, metadata)

    first = _configure(directory, state_path)
    first_profile = _read(first.profile_path)
    first_profile["userOwned"] = {"keep": "this"}
    _write_json(first.profile_path, first_profile)
    second = _configure(
        directory,
        state_path,
        models=("openai/gpt-oss-120b",),
    )

    assert second.profile_id == first.profile_id
    updated_metadata = _read(directory / desktop.META_FILENAME)
    assert updated_metadata["otherMetadata"] == {"keep": True}
    assert {entry["id"] for entry in updated_metadata["entries"]} == {
        default_id,
        existing_id,
        first.profile_id,
    }
    assert _read(directory / f"{existing_id}.json") == existing_payload
    assert _read(first.profile_path)["userOwned"] == {"keep": "this"}
    assert _read(first.profile_path)["inferenceModels"] == [{"name": "openai/gpt-oss-120b"}]
    state = _read(state_path)
    assert state["profiles"]
    assert state["profiles"][next(iter(state["profiles"]))]["owned_after"]["inferenceModels"] == [
        {"name": "openai/gpt-oss-120b"}
    ]


def test_repeat_configure_reuses_recorded_id_without_duplicate_entries(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"

    first = _configure(directory, state_path)
    before_metadata = _read(directory / desktop.META_FILENAME)
    second = _configure(directory, state_path)

    assert second.profile_id == first.profile_id
    assert second.changed is False
    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert sum(entry["name"] == "Unity Gateway" for entry in before_metadata["entries"]) == 1


def test_optional_fields_remain_owned_across_model_refresh_and_revert(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = desktop.configure_claude_desktop(
        "https://workspace.example",
        model_ids=("model-a",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command="/usr/local/bin/ug",
        helper_args=("auth-helper",),
        custom_headers={"x-fake-header": "foo"},
        always_start_with_default_model=True,
        directory=directory,
        state_path=state_path,
    )
    second = desktop.configure_claude_desktop(
        "https://workspace.example",
        model_ids=("model-b",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command="/usr/local/bin/ug",
        helper_args=("auth-helper",),
        directory=directory,
        state_path=state_path,
    )

    assert second.profile_id == first.profile_id
    refreshed = _read(first.profile_path)
    assert refreshed["inferenceCustomHeaders"] == {"x-fake-header": "foo"}
    assert refreshed["alwaysStartWithDefaultModel"] is True
    assert refreshed["inferenceModels"] == [{"name": "model-b"}]
    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        directory=directory,
        state_path=state_path,
    )
    assert reverted.changed is True
    assert not first.profile_path.exists()


def test_existing_same_name_is_not_adopted(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    directory.mkdir()
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    old_id = _uuid()
    _write_json(directory / f"{old_id}.json", {"owner": "user"})
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": old_id, "name": desktop.PROFILE_NAME})
    _write_json(directory / desktop.META_FILENAME, metadata)

    result = _configure(directory, state_path)
    updated = _read(directory / desktop.META_FILENAME)

    assert result.profile_id != old_id
    assert {entry["id"] for entry in updated["entries"]} == {default_id, old_id, result.profile_id}
    assert _read(directory / f"{old_id}.json") == {"owner": "user"}


def test_malformed_metadata_and_manifest_fail_without_writes(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    directory.mkdir()
    state_path = tmp_path / "state.json"
    (directory / desktop.META_FILENAME).write_text("not json", encoding="utf-8")

    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(directory, state_path)
    assert not state_path.exists()
    assert list(directory.iterdir()) == [directory / desktop.META_FILENAME]

    _write_json(directory / desktop.META_FILENAME, {"entries": [{"id": "bad", "name": "Default"}]})
    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(directory, state_path)
    assert not state_path.exists()


def test_configure_rejects_owned_profile_drift(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    result = _configure(directory, state_path)
    profile = _read(result.profile_path)
    profile["inferenceGatewayBaseUrl"] = "https://user-changed.example"
    _write_json(result.profile_path, profile)
    before_metadata = _read(directory / desktop.META_FILENAME)

    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(directory, state_path, models=("openai/gpt-oss-120b",))

    assert _read(result.profile_path)["inferenceGatewayBaseUrl"] == "https://user-changed.example"
    assert _read(directory / desktop.META_FILENAME) == before_metadata


def test_metadata_failure_rolls_back_profile_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "Claude-3p"
    directory.mkdir()
    default_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    before_metadata = _read(directory / desktop.META_FILENAME)
    original_atomic_write = desktop.atomic_write_json
    failed = False

    def fail_metadata_once(path: Path, payload: object) -> None:
        nonlocal failed
        if Path(path) == directory / desktop.META_FILENAME and not failed:
            failed = True
            raise OSError("simulated metadata failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(desktop, "atomic_write_json", fail_metadata_once)
    with pytest.raises(desktop.ClaudeDesktopError, match="rolled back"):
        _configure(directory, state_path)

    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert not state_path.exists()
    assert {path.name for path in directory.iterdir()} == {
        desktop.META_FILENAME,
        f"{default_id}.json",
    }
    assert _read(directory / desktop.META_FILENAME)["appliedId"] == default_id


def test_revert_restores_selection_and_keeps_user_profile_edits(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    result = _configure(directory, state_path)
    profile = _read(result.profile_path)
    profile["userOwned"] = {"keep": True}
    _write_json(result.profile_path, profile)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["metadataOwnedByUser"] = "keep"
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        directory=directory,
        state_path=state_path,
    )

    assert reverted.changed is True
    assert reverted.drifted is True
    assert _read(directory / desktop.META_FILENAME) == {
        "appliedId": default_id,
        "entries": [
            {"id": default_id, "name": "Default"},
            {"id": result.profile_id, "name": "Unity Gateway"},
        ],
        "metadataOwnedByUser": "keep",
    }
    assert _read(result.profile_path) == profile
    assert _read(state_path)["profiles"] == {}


def test_revert_preserves_drifted_owned_values_and_user_selection(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    result = _configure(directory, state_path)
    profile = _read(result.profile_path)
    profile["inferenceModels"] = [{"name": "user/model"}]
    _write_json(result.profile_path, profile)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["appliedId"] = default_id
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        directory=directory,
        state_path=state_path,
    )

    assert reverted.changed is True
    assert reverted.drifted is True
    assert _read(directory / desktop.META_FILENAME)["appliedId"] == default_id
    assert _read(directory / desktop.META_FILENAME)["entries"] == [
        {"id": default_id, "name": "Default"},
        {"id": result.profile_id, "name": "Unity Gateway"},
    ]
    assert _read(result.profile_path)["inferenceModels"] == [{"name": "user/model"}]


def test_revert_preserves_user_renamed_profile_entry(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    result = _configure(directory, state_path)
    metadata = _read(directory / desktop.META_FILENAME)
    next(entry for entry in metadata["entries"] if entry["id"] == result.profile_id)["name"] = (
        "User renamed"
    )
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        directory=directory,
        state_path=state_path,
    )

    assert reverted.changed is True
    updated = _read(directory / desktop.META_FILENAME)
    assert {entry["name"] for entry in updated["entries"]} == {"Default", "User renamed"}
    assert result.profile_path.exists()


def test_config_library_store_is_selected_and_ambiguous_stores_fail(tmp_path: Path) -> None:
    root = tmp_path / "Claude-3p"
    library = root / "configLibrary"
    library.mkdir(parents=True)
    _seed_metadata(library)
    state_path = tmp_path / "state.json"

    result = _configure(root, state_path)
    assert result.metadata_path.parent == library
    assert result.profile_path.parent == library
    assert not (root / desktop.META_FILENAME).exists()

    _write_json(root / desktop.META_FILENAME, _read(library / desktop.META_FILENAME))
    with pytest.raises(desktop.ClaudeDesktopConflictError, match="both"):
        _configure(root, tmp_path / "other-state.json")


def test_existing_config_library_directory_is_used_before_creating_metadata(tmp_path: Path) -> None:
    root = tmp_path / "Claude-3p"
    (root / "configLibrary").mkdir(parents=True)

    result = _configure(root, tmp_path / "state.json")

    assert result.metadata_path == root / "configLibrary" / desktop.META_FILENAME
    assert not (root / desktop.META_FILENAME).exists()


def test_native_resolution_is_macos_only_and_does_not_install_missing_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert desktop.claude_desktop_directory(platform="linux") is None
    assert desktop.claude_desktop_directory(platform="win32") is None
    monkeypatch.setattr(desktop, "claude_desktop_directory", lambda: tmp_path / "missing")
    monkeypatch.setattr(desktop, "_claude_desktop_installed", lambda: False)

    with pytest.raises(desktop.ClaudeDesktopUnavailableError, match="will not install"):
        desktop.configure_claude_desktop(
            "https://workspace.example",
            model_ids=("model",),
            base_url="https://workspace.example/ai-gateway/anthropic",
            helper_command="/usr/local/bin/ug",
            helper_args=("claude-desktop-auth", "--host", "host", "--profile", "selectedprofile"),
        )
    assert not (tmp_path / "missing").exists()


def test_fresh_metadata_has_no_dangling_default_and_revert_removes_it(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    configured = _configure(directory, state_path)
    metadata = _read(configured.metadata_path)
    for entry in metadata["entries"]:
        assert (directory / f"{entry['id']}.json").is_file()
    desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )
    assert not configured.profile_path.exists()
    assert not configured.metadata_path.exists()


def test_reverse_workspace_revert_does_not_restore_removed_profile(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = _configure(directory, state_path, workspace="https://first.example")
    second = _configure(directory, state_path, workspace="https://second.example")
    desktop.revert_claude_desktop(
        "https://first.example", directory=directory, state_path=state_path
    )
    assert not first.profile_path.exists()
    desktop.revert_claude_desktop(
        "https://second.example", directory=directory, state_path=state_path
    )
    metadata = _read(second.metadata_path)
    assert metadata.get("appliedId") != first.profile_id
    if metadata.get("appliedId"):
        assert (directory / f"{metadata['appliedId']}.json").is_file()


@pytest.mark.parametrize("failed_file", ["profile", "metadata", "manifest"])
def test_each_write_failure_preserves_existing_files(tmp_path, monkeypatch, failed_file):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    write = desktop.atomic_write_json
    failed = False

    def fail_once(path, payload):
        nonlocal failed
        target = (
            path == state_path
            if failed_file == "manifest"
            else path.name == desktop.META_FILENAME
            if failed_file == "metadata"
            else path.name != desktop.META_FILENAME and path.parent == directory
        )
        if target and not failed:
            failed = True
            raise OSError("simulated write failure")
        write(path, payload)

    monkeypatch.setattr(desktop, "atomic_write_json", fail_once)
    with pytest.raises(desktop.ClaudeDesktopError, match="rolled back"):
        _configure(directory, state_path)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert not state_path.exists()


def test_revert_without_ownership_creates_no_desktop_files(tmp_path):
    directory = tmp_path / "missing-desktop"
    state_path = tmp_path / "missing-ownership.json"
    result = desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )
    assert not result.changed
    assert not directory.exists()
    assert not state_path.exists()
    _write_json(state_path, {"version": desktop.MANIFEST_VERSION, "profiles": {}})
    result = desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )
    assert not result.changed
    assert not directory.exists()


def test_revert_missing_renamed_profile_reports_conflict_and_preserves_metadata(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    configured = _configure(directory, state_path)
    metadata = _read(configured.metadata_path)
    next(entry for entry in metadata["entries"] if entry["id"] == configured.profile_id)["name"] = (
        "Personal"
    )
    _write_json(configured.metadata_path, metadata)
    configured.profile_path.unlink()
    before = configured.metadata_path.read_bytes()
    with pytest.raises(desktop.ClaudeDesktopConflictError, match="profile file is missing"):
        desktop.revert_claude_desktop(
            "https://workspace.example", directory=directory, state_path=state_path
        )
    assert configured.metadata_path.read_bytes() == before
    assert _read(state_path)["profiles"]
