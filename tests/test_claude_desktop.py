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
    profile_key: str | None = None,
) -> desktop.ConfigureResult:
    return desktop.configure_claude_desktop(
        workspace,
        model_ids=models,
        base_url=f"{workspace}/ai-gateway/anthropic",
        helper_command="/Applications/Unity Gateway/bin/ug with spaces",
        helper_args=(
            "claude-desktop-auth",
            "--host",
            "host",
            "--profile",
            profile_key or "selectedprofile",
        ),
        directory=directory,
        state_path=state_path,
        profile_key=profile_key,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_legacy_profile(directory: Path, state_path: Path) -> str:
    configured = _configure(directory, state_path)
    legacy_id = _uuid()
    configured.profile_path.rename(directory / f"{legacy_id}.json")
    metadata = _read(configured.metadata_path)
    metadata["appliedId"] = legacy_id
    next(entry for entry in metadata["entries"] if entry["id"] == configured.profile_id)["id"] = (
        legacy_id
    )
    _write_json(configured.metadata_path, metadata)
    manifest = _read(state_path)
    record = next(iter(manifest["profiles"].values()))
    record["profile_id"] = legacy_id
    record["entry_after"]["id"] = legacy_id
    record["workspace"] = "https://WORKSPACE.example/"
    record["profile_key"] = "old-profile"
    legacy_scope = json.dumps(
        [str(directory), record["workspace"], "old-profile"], separators=(",", ":")
    )
    manifest["profiles"] = {legacy_scope: record}
    _write_json(state_path, manifest)
    return legacy_id


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


@pytest.mark.parametrize(
    "workspace",
    ["https://WORKSPACE.example:443///", " workspace.example/ ", "HTTPS://workspace.example"],
)
def test_workspace_id_is_deterministic_across_machines_and_url_spellings(tmp_path, workspace):
    first = _configure(tmp_path / "first", tmp_path / "first-state.json", workspace=workspace)
    second = _configure(tmp_path / "second", tmp_path / "second-state.json")
    third = _configure(
        tmp_path / "third", tmp_path / "third-state.json", workspace="https://other.example"
    )

    assert first.profile_id == second.profile_id
    assert second.profile_id == "142dc87e-3afe-591e-acf7-244d8e755a93"
    assert uuid.UUID(first.profile_id).version == 5
    assert third.profile_id != first.profile_id
    assert _read(first.metadata_path)["entries"] == [
        {"id": first.profile_id, "name": "Unity Gateway"}
    ]


def test_same_workspace_with_another_auth_profile_updates_one_desktop_profile(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    first = _configure(directory, state_path, profile_key="first")
    second = _configure(directory, state_path, profile_key="second", models=("model-b",))

    assert second.profile_id == first.profile_id
    assert _read(second.profile_path)["inferenceCredentialHelperArgs"][-1] == "second"
    assert _read(second.profile_path)["inferenceModels"] == [{"name": "model-b"}]
    assert _read(second.metadata_path)["entries"] == [
        {"id": second.profile_id, "name": "Unity Gateway"}
    ]
    assert len(_read(state_path)["profiles"]) == 1
    desktop.revert_claude_desktop(
        "https://WORKSPACE.example/",
        directory=directory,
        state_path=state_path,
        profile_key="first",
    )
    assert not second.profile_path.exists()


@pytest.mark.parametrize("deleted", ["profile", "entry", "both", "metadata"])
def test_configure_recreates_deleted_owned_profile_and_registration(tmp_path, deleted):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    first = _configure(directory, state_path)
    if deleted in ("profile", "both"):
        first.profile_path.unlink()
    if deleted in ("entry", "both"):
        metadata = _read(first.metadata_path)
        metadata["entries"] = [
            entry for entry in metadata["entries"] if entry["id"] != first.profile_id
        ]
        metadata["appliedId"] = default_id
        _write_json(first.metadata_path, metadata)
    if deleted == "metadata":
        first.metadata_path.unlink()

    second = _configure(directory, state_path, models=("model-b",))

    assert second.profile_id == first.profile_id
    assert _read(second.profile_path)["inferenceModels"] == [{"name": "model-b"}]
    metadata = _read(second.metadata_path)
    assert metadata["appliedId"] == second.profile_id
    assert sum(entry["id"] == second.profile_id for entry in metadata["entries"]) == 1
    assert (directory / f"{default_id}.json").exists()


def test_recreated_profile_revert_restores_selection_at_recreation(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = _configure(directory, state_path)
    first.profile_path.unlink()
    selected_id = _seed_metadata(directory, name="Selected after deletion")
    second = _configure(directory, state_path)

    desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )

    assert _read(second.metadata_path)["appliedId"] == selected_id
    assert not second.profile_path.exists()


@pytest.mark.parametrize("collision", ["file", "entry"])
def test_matching_deterministic_id_without_ownership_is_not_adopted(tmp_path, collision):
    isolated = _configure(tmp_path / "isolated", tmp_path / "isolated-state.json")
    directory = tmp_path / "Claude-3p"
    _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    if collision == "file":
        _write_json(directory / f"{isolated.profile_id}.json", {"owner": "user"})
    else:
        metadata = _read(directory / desktop.META_FILENAME)
        metadata["entries"].append({"id": isolated.profile_id, "name": "Unity Gateway"})
        _write_json(directory / desktop.META_FILENAME, metadata)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    with pytest.raises(desktop.ClaudeDesktopConflictError, match="without matching UG ownership"):
        _configure(directory, state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert not state_path.exists()


@pytest.mark.parametrize("deleted", [False, True])
def test_random_id_is_migrated_without_duplicate_profiles(tmp_path, deleted):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)
    legacy_path = directory / f"{legacy_id}.json"
    if deleted:
        legacy_path.unlink()
        metadata = _read(directory / desktop.META_FILENAME)
        metadata["entries"] = [entry for entry in metadata["entries"] if entry["id"] != legacy_id]
        metadata["appliedId"] = default_id
        _write_json(directory / desktop.META_FILENAME, metadata)
    else:
        profile = _read(legacy_path)
        profile["userOwned"] = {"keep": True}
        profile["inferenceModels"] = [{"name": "user-model"}]
        profile["inferenceGatewayBaseUrl"] = "https://user-changed.example"
        _write_json(legacy_path, profile)

    result = _configure(directory, state_path, models=("model-b",), profile_key="new-profile")

    assert uuid.UUID(result.profile_id).version == 5
    assert result.profile_id != legacy_id
    assert not legacy_path.exists()
    assert _read(result.metadata_path) == {
        "appliedId": result.profile_id,
        "entries": [
            {"id": default_id, "name": "Default"},
            {"id": result.profile_id, "name": "Unity Gateway"},
        ],
    }
    assert len(_read(state_path)["profiles"]) == 1
    assert _read(result.profile_path)["inferenceModels"] == [{"name": "model-b"}]
    assert _read(result.profile_path)["inferenceGatewayBaseUrl"] == (
        "https://workspace.example/ai-gateway/anthropic"
    )
    if not deleted:
        assert _read(result.profile_path)["userOwned"] == {"keep": True}
    assert (
        _configure(directory, state_path, models=("model-b",), profile_key="new-profile").changed
        is False
    )


def test_migration_updates_other_workspaces_previous_selection(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)
    other = _configure(directory, state_path, workspace="https://other.example")
    migrated = _configure(directory, state_path)
    metadata = _read(migrated.metadata_path)
    metadata["appliedId"] = other.profile_id
    _write_json(migrated.metadata_path, metadata)

    desktop.revert_claude_desktop(
        "https://other.example", directory=directory, state_path=state_path
    )

    assert _read(migrated.metadata_path)["appliedId"] == migrated.profile_id
    assert migrated.profile_path.exists()
    assert not (directory / f"{legacy_id}.json").exists()


@pytest.mark.parametrize("drift", ["renamed-entry", "target-collision"])
def test_migration_conflicts_preserve_every_existing_file(tmp_path, drift):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)
    if drift == "renamed-entry":
        metadata = _read(directory / desktop.META_FILENAME)
        next(entry for entry in metadata["entries"] if entry["id"] == legacy_id)["name"] = "Renamed"
        _write_json(directory / desktop.META_FILENAME, metadata)
    else:
        isolated = _configure(tmp_path / "isolated", tmp_path / "isolated-state.json")
        _write_json(directory / f"{isolated.profile_id}.json", {"owner": "user"})
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes()

    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(directory, state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("failed_file", ["profile", "metadata", "manifest", "retired-profile"])
def test_migration_write_failures_restore_original_profile_metadata_and_manifest(
    tmp_path, monkeypatch, failed_file
):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes()
    write = desktop._write_or_remove
    failed = False

    def fail_once(path, payload):
        nonlocal failed
        target = (
            path == state_path
            if failed_file == "manifest"
            else path.name == desktop.META_FILENAME
            if failed_file == "metadata"
            else path.name == f"{legacy_id}.json"
            if failed_file == "retired-profile"
            else payload is not None
            and path.parent == directory
            and path.name != desktop.META_FILENAME
        )
        if target and not failed:
            failed = True
            raise OSError("simulated migration failure")
        write(path, payload)

    monkeypatch.setattr(desktop, "_write_or_remove", fail_once)
    with pytest.raises(desktop.ClaudeDesktopError, match="rolled back"):
        _configure(directory, state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state_path.read_bytes() == state_before


def test_revert_handles_legacy_scope_without_migrating_first(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )

    assert reverted.profile_id == legacy_id
    assert not (directory / f"{legacy_id}.json").exists()
    assert _read(directory / desktop.META_FILENAME)["appliedId"] == default_id
    assert _read(state_path)["profiles"] == {}


def test_multiple_legacy_profiles_fail_without_overwriting_any_profile(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)
    duplicate_id = _uuid()
    _write_json(directory / f"{duplicate_id}.json", _read(directory / f"{legacy_id}.json"))
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": duplicate_id, "name": "Unity Gateway"})
    _write_json(directory / desktop.META_FILENAME, metadata)
    manifest = _read(state_path)
    duplicate_record = json.loads(json.dumps(next(iter(manifest["profiles"].values()))))
    duplicate_record["profile_id"] = duplicate_id
    duplicate_record["entry_after"]["id"] = duplicate_id
    manifest["profiles"]["another-legacy-profile"] = duplicate_record
    _write_json(state_path, manifest)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes()

    with pytest.raises(desktop.ClaudeDesktopConflictError, match="Multiple UG-owned"):
        _configure(directory, state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state_path.read_bytes() == state_before


def test_malformed_record_cannot_be_overwritten_by_deterministic_configuration(tmp_path):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    configured = _configure(directory, state_path)
    manifest = _read(state_path)
    scope = next(iter(manifest["profiles"]))
    manifest["profiles"][scope] = None
    _write_json(state_path, manifest)
    configured.profile_path.unlink()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes()

    with pytest.raises(desktop.ClaudeDesktopConflictError, match="malformed"):
        _configure(directory, state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("change", ["unchanged", "edited", "deleted"])
@pytest.mark.parametrize(
    "refresh_headers,refresh_default",
    [(None, None), ({"x-new-header": "refreshed"}, False)],
    ids=["preserved", "replaced"],
)
def test_optional_fields_remain_owned_across_model_refresh_and_revert(
    tmp_path: Path,
    change: str,
    refresh_headers: dict[str, str] | None,
    refresh_default: bool | None,
) -> None:
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
    if change != "unchanged":
        profile = _read(first.profile_path)
        if change == "edited":
            profile["inferenceCustomHeaders"] = {"x-user-changed": "bar"}
            profile["alwaysStartWithDefaultModel"] = False
        else:
            for key in desktop.OPTIONAL_PROFILE_KEYS:
                profile.pop(key)
        _write_json(first.profile_path, profile)
    second = desktop.configure_claude_desktop(
        "https://workspace.example",
        model_ids=("model-b",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command="/usr/local/bin/ug",
        helper_args=("auth-helper",),
        custom_headers=refresh_headers,
        always_start_with_default_model=refresh_default,
        directory=directory,
        state_path=state_path,
    )

    assert second.profile_id == first.profile_id
    refreshed = _read(first.profile_path)
    assert refreshed["inferenceCustomHeaders"] == (
        refresh_headers if refresh_headers is not None else {"x-fake-header": "foo"}
    )
    assert refreshed["alwaysStartWithDefaultModel"] is (
        refresh_default if refresh_default is not None else True
    )
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


@pytest.mark.parametrize("key", desktop.OWNED_PROFILE_KEYS)
@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_configure_refreshes_edited_and_deleted_owned_fields(
    tmp_path: Path, key: str, change: str
) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    default_path = directory / f"{default_id}.json"
    default_before = default_path.read_bytes()
    result = _configure(directory, state_path)
    expected = _read(result.profile_path)
    expected["userOwned"] = {"keep": True}
    expected["alwaysStartWithDefaultModel"] = False
    expected["inferenceCustomHeaders"] = {"x-personal": "keep"}
    profile = dict(expected)
    if change == "edited":
        profile[key] = None
    else:
        profile.pop(key)
    _write_json(result.profile_path, profile)
    before_metadata = _read(directory / desktop.META_FILENAME)

    refreshed = _configure(directory, state_path, models=("openai/gpt-oss-120b",))

    expected["inferenceModels"] = [{"name": "openai/gpt-oss-120b"}]
    assert refreshed.profile_id == result.profile_id
    assert refreshed.changed is True
    assert _read(result.profile_path) == expected
    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert default_path.read_bytes() == default_before
    record = next(iter(_read(state_path)["profiles"].values()))
    assert record["owned_after"] == {key: expected[key] for key in desktop.OWNED_PROFILE_KEYS}
    assert _configure(directory, state_path, models=("openai/gpt-oss-120b",)).changed is False


def test_revert_after_refresh_removes_reapplied_owned_profile(tmp_path: Path) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    configured = _configure(directory, state_path)
    profile = _read(configured.profile_path)
    profile["inferenceModels"] = [{"name": "user-model"}]
    _write_json(configured.profile_path, profile)

    _configure(directory, state_path, models=("new-model",))
    reverted = desktop.revert_claude_desktop(
        "https://workspace.example", directory=directory, state_path=state_path
    )

    assert reverted.changed is True
    assert reverted.drifted is False
    assert not configured.profile_path.exists()
    assert _read(configured.metadata_path) == {
        "appliedId": default_id,
        "entries": [{"id": default_id, "name": "Default"}],
    }
    assert _read(state_path)["profiles"] == {}


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


@pytest.mark.parametrize("reconfigure", [False, True])
def test_revert_preserves_drifted_owned_values_and_user_selection(
    tmp_path: Path, reconfigure: bool
) -> None:
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    result = _configure(directory, state_path)
    if reconfigure:
        _configure(directory, state_path, models=("new-model",))
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
@pytest.mark.parametrize("reconfigure", [False, True])
def test_each_write_failure_preserves_existing_files(
    tmp_path, monkeypatch, failed_file, reconfigure
):
    directory = tmp_path / "Claude-3p"
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    if reconfigure:
        configured = _configure(directory, state_path)
        profile = _read(configured.profile_path)
        profile["inferenceModels"] = [{"name": "user-model"}]
        profile["userOwned"] = {"keep": True}
        _write_json(configured.profile_path, profile)
        metadata = _read(configured.metadata_path)
        metadata["appliedId"] = default_id
        _write_json(configured.metadata_path, metadata)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes() if state_path.exists() else None
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
        _configure(directory, state_path, models=("new-model",))
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    if state_before is None:
        assert not state_path.exists()
    else:
        assert state_path.read_bytes() == state_before


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
