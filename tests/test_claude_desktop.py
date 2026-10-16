"""Filesystem contract tests for Claude Desktop profile configuration."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode.agents.desktop import claude as desktop
from ucode.agents.desktop import claude_config as desktop_config


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
        state_path=state_path,
        profile_key=profile_key,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_legacy_profile(directory: Path, state_path: Path) -> str:
    configured = _configure(state_path)
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


def test_render_profile_accepts_custom_headers(tmp_path: Path) -> None:
    profile = desktop.render_profile(
        model_ids=("system.ai.claude-opus-5-5",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command=tmp_path / "ug",
        helper_args=("auth-helper",),
        custom_headers={"x-fake-header": "foo"},
    )

    assert profile["inferenceCustomHeaders"] == {"x-fake-header": "foo"}
    assert "alwaysStartWithDefaultModel" not in profile
    assert profile["inferenceModels"] == [{"name": "system.ai.claude-opus-5-5"}]


def test_render_profile_preserves_model_order_duplicates_and_literal_helper_args(tmp_path):
    model_ids = ["model-b", "model-a", "model-b"]
    helper_args = ["auth-helper", "--profile", "profile with spaces"]
    headers = {"x-team": "aig"}

    profile = desktop.render_profile(
        model_ids=model_ids,
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command=tmp_path / "ug",
        helper_args=helper_args,
        custom_headers=headers,
    )
    model_ids.append("later-model")
    helper_args.append("later-arg")
    headers["x-team"] = "changed"

    assert profile["inferenceModels"] == [
        {"name": "model-b"},
        {"name": "model-a"},
        {"name": "model-b"},
    ]
    assert profile["inferenceCredentialHelperArgs"] == [
        "auth-helper",
        "--profile",
        "profile with spaces",
    ]
    assert profile["inferenceCustomHeaders"] == {"x-team": "aig"}


def test_configure_creates_profile_and_metadata_in_native_library(
    tmp_path: Path,
    directory: Path,
) -> None:
    state_path = tmp_path / "ucode" / "claude-desktop.json"

    result = _configure(state_path)

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
    directory: Path,
) -> None:
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

    first = _configure(state_path)
    first_profile = _read(first.profile_path)
    first_profile["userOwned"] = {"keep": "this"}
    _write_json(first.profile_path, first_profile)
    second = _configure(
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


def test_repeat_configure_reuses_recorded_id_without_duplicate_entries(
    tmp_path: Path, directory: Path
) -> None:
    state_path = tmp_path / "state.json"

    first = _configure(state_path)
    before_metadata = _read(directory / desktop.META_FILENAME)
    second = _configure(state_path)

    assert second.profile_id == first.profile_id
    assert second.changed is False
    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert sum(entry["name"] == "Unity Gateway" for entry in before_metadata["entries"]) == 1


def test_repeat_configure_reuses_existing_workspace_scoped_revert_record(tmp_path, directory):
    prior_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    configured = _configure(state_path)
    manifest = _read(state_path)
    record = next(iter(manifest["profiles"].values()))
    old_scope = json.dumps([str(directory), "https://workspace.example"], separators=(",", ":"))
    manifest["profiles"] = {old_scope: record}
    _write_json(state_path, manifest)

    refreshed = _configure(state_path, models=("updated-model",))

    records = _read(state_path)["profiles"]
    assert set(records) == {old_scope}
    assert records[old_scope]["profile_before"] == record["profile_before"]
    assert records[old_scope]["prior_applied"] == prior_id
    assert refreshed.profile_id == configured.profile_id

    desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert not configured.profile_path.exists()
    assert _read(configured.metadata_path)["appliedId"] == prior_id


@pytest.mark.parametrize(
    "workspace",
    ["https://WORKSPACE.example:443///", " workspace.example/ ", "HTTPS://workspace.example"],
)
def test_workspace_id_is_deterministic_across_machines_and_url_spellings(
    tmp_path, workspace, directory, monkeypatch
):
    relative_directory = directory.relative_to(tmp_path)
    results = []
    for machine, url in (
        ("first", workspace),
        ("second", "https://workspace.example"),
        ("third", "https://other.example"),
    ):
        machine_root = tmp_path / machine
        home = machine_root / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("LOCALAPPDATA", str(machine_root / "relocated local app data"))
        expected_directory = machine_root / relative_directory
        expected_directory.parent.mkdir(parents=True)

        result = _configure(machine_root / "state.json", workspace=url)

        assert result.profile_path.parent == expected_directory
        results.append(result)
    first, second, third = results

    assert first.profile_id == second.profile_id
    assert second.profile_id == "142dc87e-3afe-591e-acf7-244d8e755a93"
    assert uuid.UUID(first.profile_id).version == 5
    assert third.profile_id != first.profile_id
    assert _read(first.metadata_path)["entries"] == [
        {"id": first.profile_id, "name": "Unity Gateway"}
    ]


def test_workspace_url_spellings_share_ownership_through_refresh_and_revert(
    tmp_path, directory: Path
):
    directory.mkdir()
    prior_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"

    first = _configure(state_path, workspace="https://WORKSPACE.example:443/")
    second = _configure(state_path, models=("updated-model",))

    assert first.profile_id == second.profile_id == "142dc87e-3afe-591e-acf7-244d8e755a93"
    assert _read(second.profile_path)["inferenceModels"] == [{"name": "updated-model"}]
    records = _read(state_path)["profiles"]
    assert len(records) == 1
    assert next(iter(records.values()))["workspace"] == "https://workspace.example"

    result = desktop.revert_claude_desktop(" workspace.example/ ", state_path=state_path)

    assert result.changed is True
    assert not second.profile_path.exists()
    assert _read(second.metadata_path) == {
        "appliedId": prior_id,
        "entries": [{"id": prior_id, "name": "Default"}],
    }
    assert _read(state_path)["profiles"] == {}


def test_same_workspace_with_another_auth_profile_updates_one_desktop_profile(
    tmp_path, directory: Path
):
    state_path = tmp_path / "state.json"
    first = _configure(state_path, profile_key="first")
    second = _configure(state_path, profile_key="second", models=("model-b",))

    assert second.profile_id == first.profile_id
    assert _read(second.profile_path)["inferenceCredentialHelperArgs"][-1] == "second"
    assert _read(second.profile_path)["inferenceModels"] == [{"name": "model-b"}]
    assert _read(second.metadata_path)["entries"] == [
        {"id": second.profile_id, "name": "Unity Gateway - second"}
    ]
    assert len(_read(state_path)["profiles"]) == 1
    desktop.revert_claude_desktop(
        "https://WORKSPACE.example/",
        state_path=state_path,
        profile_key="first",
    )
    assert not second.profile_path.exists()


@pytest.mark.parametrize("profile_key", ["team-profile", "team profile", "研发"])
def test_profile_label_includes_databricks_profile_and_reverts(tmp_path, directory, profile_key):
    default_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"

    configured = _configure(state_path, profile_key=profile_key)

    entry = {"id": configured.profile_id, "name": f"Unity Gateway - {profile_key}"}
    assert _read(configured.metadata_path)["entries"] == [
        {"id": default_id, "name": "Default"},
        entry,
    ]
    assert next(iter(_read(state_path)["profiles"].values()))["entry_after"] == entry
    assert not _configure(state_path, profile_key=profile_key).changed

    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert reverted.changed
    assert not configured.profile_path.exists()
    assert _read(configured.metadata_path) == {
        "appliedId": default_id,
        "entries": [{"id": default_id, "name": "Default"}],
    }


def test_reconfigure_updates_legacy_generic_label_without_changing_id(tmp_path, directory):
    state_path = tmp_path / "state.json"
    configured = _configure(state_path)

    refreshed = _configure(state_path, profile_key="team-profile")

    assert refreshed.profile_id == configured.profile_id
    assert _read(refreshed.metadata_path)["entries"] == [
        {"id": refreshed.profile_id, "name": "Unity Gateway - team-profile"}
    ]
    assert len(_read(state_path)["profiles"]) == 1


def test_reconfigure_replaces_user_renamed_managed_entry(tmp_path, directory):
    state_path = tmp_path / "state.json"
    configured = _configure(state_path, profile_key="team-profile")
    metadata = _read(configured.metadata_path)
    metadata["entries"][0]["name"] = "My renamed gateway"
    _write_json(configured.metadata_path, metadata)

    refreshed = _configure(state_path, profile_key="other-profile")

    assert refreshed.profile_id == configured.profile_id
    assert _read(refreshed.metadata_path)["entries"] == [
        {"id": refreshed.profile_id, "name": "Unity Gateway - other-profile"}
    ]

    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert not reverted.drifted
    assert not configured.profile_path.exists()


@pytest.mark.parametrize("operation", ["configure", "revert"])
def test_profile_lock_timeout_preserves_files_and_closes_handle(
    tmp_path, directory, monkeypatch, operation
):
    state_path = tmp_path / "state.json"
    if operation == "revert":
        _configure(state_path)
    before = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    state_before = state_path.read_bytes() if state_path.exists() else None
    handles = []

    def contended_lock(handle, *, timeout=None):
        assert timeout == 5
        handles.append(handle)
        raise TimeoutError("lock is held")

    monkeypatch.setattr(desktop_config, "acquire_exclusive_file_lock", contended_lock)
    monkeypatch.setattr(
        desktop_config, "release_file_lock", lambda *_args: pytest.fail("unacquired lock released")
    )

    with pytest.raises(desktop.ClaudeDesktopError, match="Timed out"):
        if operation == "configure":
            _configure(state_path)
        else:
            desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert handles[0].closed
    assert {path.name: path.read_bytes() for path in directory.glob("*.json")} == before
    assert (state_path.read_bytes() if state_path.exists() else None) == state_before


@pytest.mark.parametrize("deleted", ["profile", "entry", "both", "metadata"])
def test_configure_recreates_deleted_owned_profile_and_registration(
    tmp_path, deleted, directory: Path
):
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    first = _configure(state_path)
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

    second = _configure(state_path, models=("model-b",))

    assert second.profile_id == first.profile_id
    assert _read(second.profile_path)["inferenceModels"] == [{"name": "model-b"}]
    metadata = _read(second.metadata_path)
    assert metadata["appliedId"] == second.profile_id
    assert sum(entry["id"] == second.profile_id for entry in metadata["entries"]) == 1
    assert (directory / f"{default_id}.json").exists()


def test_recreated_profile_revert_restores_selection_at_recreation(tmp_path, directory: Path):
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = _configure(state_path)
    first.profile_path.unlink()
    selected_id = _seed_metadata(directory, name="Selected after deletion")
    second = _configure(state_path)

    desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert _read(second.metadata_path)["appliedId"] == selected_id
    assert not second.profile_path.exists()


@pytest.mark.parametrize("existing", ["file", "entry", "both"])
def test_generated_uuid_is_overwritten_without_ownership_record(
    tmp_path, existing, directory: Path
):
    profile_id = str(uuid.uuid5(desktop.PROFILE_NAMESPACE, "https://workspace.example"))
    prior_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    profile_before = {"inferenceProvider": "old-provider", "userOwned": {"keep": True}}
    if existing in ("file", "both"):
        _write_json(directory / f"{profile_id}.json", profile_before)
    if existing in ("entry", "both"):
        metadata = _read(directory / desktop.META_FILENAME)
        metadata["entries"].append({"id": profile_id, "name": "Old label", "extra": "keep"})
        _write_json(directory / desktop.META_FILENAME, metadata)
    metadata_before = _read(directory / desktop.META_FILENAME)
    unrelated_before = (directory / f"{prior_id}.json").read_bytes()

    result = _configure(state_path, profile_key="team-profile")

    assert result.profile_id == profile_id
    assert _read(result.profile_path)["inferenceProvider"] == "gateway"
    assert _read(result.metadata_path) == {
        "appliedId": result.profile_id,
        "entries": [
            {"id": prior_id, "name": "Default"},
            {"id": result.profile_id, "name": "Unity Gateway - team-profile"},
        ],
    }
    if existing in ("file", "both"):
        assert _read(result.profile_path)["userOwned"] == {"keep": True}

    desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    if existing in ("file", "both"):
        assert _read(result.profile_path) == profile_before
    else:
        assert not result.profile_path.exists()
    expected_metadata = json.loads(json.dumps(metadata_before))
    if existing == "file":
        expected_metadata["entries"].append(
            {"id": profile_id, "name": "Unity Gateway - team-profile"}
        )
    assert _read(result.metadata_path) == expected_metadata
    assert (directory / f"{prior_id}.json").read_bytes() == unrelated_before
    assert _read(state_path)["profiles"] == {}


@pytest.mark.parametrize("selected", ["default", "managed"])
@pytest.mark.parametrize("deleted", [False, True])
def test_repeat_configure_retains_original_profile_and_metadata_baselines(
    tmp_path, directory, selected, deleted
):
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    profile_id = str(uuid.uuid5(desktop.PROFILE_NAMESPACE, "https://workspace.example"))
    profile_path = directory / f"{profile_id}.json"
    profile_before = {"inferenceProvider": "original", "inferenceModels": [{"name": "original"}]}
    _write_json(profile_path, profile_before)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": profile_id, "name": "Original name"})
    metadata["otherMetadata"] = {"keep": True}
    if selected == "managed":
        metadata["appliedId"] = profile_id
    _write_json(directory / desktop.META_FILENAME, metadata)

    _configure(state_path, profile_key="first")
    first_record = next(iter(_read(state_path)["profiles"].values()))
    if deleted:
        profile_path.unlink()
    refreshed = _configure(state_path, profile_key="second", models=("updated-model",))
    record = next(iter(_read(state_path)["profiles"].values()))

    assert refreshed.profile_id == profile_id
    assert record["profile_before"] == first_record["profile_before"] == profile_before
    assert record["entry_before"] == first_record["entry_before"] == metadata["entries"][-1]
    assert _read(refreshed.profile_path)["inferenceModels"] == [{"name": "updated-model"}]
    assert not _configure(state_path, profile_key="second", models=("updated-model",)).changed

    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert not reverted.drifted
    assert _read(profile_path) == profile_before
    assert _read(refreshed.metadata_path) == metadata
    assert _read(state_path)["profiles"] == {}


def test_revert_preserves_later_edits_to_preexisting_uuid_profile(tmp_path, directory):
    prior_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    profile_id = str(uuid.uuid5(desktop.PROFILE_NAMESPACE, "https://workspace.example"))
    profile_path = directory / f"{profile_id}.json"
    _write_json(profile_path, {"inferenceProvider": "original", "inferenceModels": []})
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": profile_id, "name": "Original label"})
    _write_json(directory / desktop.META_FILENAME, metadata)
    configured = _configure(state_path)
    profile = _read(profile_path)
    profile["inferenceModels"] = [{"name": "user-model"}]
    profile["userOwned"] = "keep"
    _write_json(profile_path, profile)
    current_metadata = _read(configured.metadata_path)
    current_metadata["appliedId"] = prior_id
    _write_json(configured.metadata_path, current_metadata)

    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert reverted.drifted
    assert _read(profile_path) == {
        "inferenceProvider": "original",
        "inferenceModels": [{"name": "user-model"}],
        "userOwned": "keep",
    }
    assert _read(configured.metadata_path) == metadata


def test_revert_handles_legacy_scope_without_migrating_first(tmp_path, directory: Path):
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    legacy_id = _seed_legacy_profile(directory, state_path)

    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert reverted.profile_id == legacy_id
    assert not (directory / f"{legacy_id}.json").exists()
    assert _read(directory / desktop.META_FILENAME)["appliedId"] == default_id
    assert _read(state_path)["profiles"] == {}


def test_configure_leaves_random_id_profiles_and_records_untouched(tmp_path, directory: Path):
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
    other = _configure(state_path, workspace="https://other.example")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    records_before = _read(state_path)["profiles"]
    metadata_before = _read(directory / desktop.META_FILENAME)

    configured = _configure(state_path, profile_key="new-profile")

    assert configured.profile_id not in (legacy_id, duplicate_id, other.profile_id)
    for name, contents in before.items():
        if name != desktop.META_FILENAME:
            assert (directory / name).read_bytes() == contents
    records = _read(state_path)["profiles"]
    assert len(records) == len(records_before) + 1
    for scope, record in records_before.items():
        assert records[scope] == record
    metadata = _read(configured.metadata_path)
    assert metadata["entries"][:-1] == metadata_before["entries"]
    assert metadata["appliedId"] == configured.profile_id

    desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert not configured.profile_path.exists()
    assert _read(configured.metadata_path) == metadata_before
    assert _read(state_path)["profiles"] == records_before

    desktop.revert_claude_desktop("https://other.example", state_path=state_path)

    assert _read(configured.metadata_path)["appliedId"] == legacy_id
    assert (directory / f"{legacy_id}.json").read_bytes() == before[f"{legacy_id}.json"]
    assert (directory / f"{duplicate_id}.json").read_bytes() == before[f"{duplicate_id}.json"]


def test_malformed_record_cannot_be_overwritten_by_deterministic_configuration(
    tmp_path, directory: Path
):
    state_path = tmp_path / "state.json"
    configured = _configure(state_path)
    manifest = _read(state_path)
    scope = next(iter(manifest["profiles"]))
    manifest["profiles"][scope] = None
    _write_json(state_path, manifest)
    configured.profile_path.unlink()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes()

    with pytest.raises(desktop.ClaudeDesktopConflictError, match="malformed"):
        _configure(state_path)

    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert state_path.read_bytes() == state_before


@pytest.mark.parametrize("change", ["unchanged", "edited", "deleted"])
@pytest.mark.parametrize(
    "refresh_headers",
    [None, {"x-new-header": "refreshed"}],
    ids=["preserved", "replaced"],
)
def test_custom_headers_remain_owned_across_model_refresh_and_revert(
    tmp_path: Path,
    change: str,
    refresh_headers: dict[str, str] | None,
    directory: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = desktop.configure_claude_desktop(
        "https://workspace.example",
        model_ids=("model-a",),
        base_url="https://workspace.example/ai-gateway/anthropic",
        helper_command="/usr/local/bin/ug",
        helper_args=("auth-helper",),
        custom_headers={"x-fake-header": "foo"},
        state_path=state_path,
    )
    if change != "unchanged":
        profile = _read(first.profile_path)
        if change == "edited":
            profile["inferenceCustomHeaders"] = {"x-user-changed": "bar"}
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
        state_path=state_path,
    )

    assert second.profile_id == first.profile_id
    refreshed = _read(first.profile_path)
    assert refreshed["inferenceCustomHeaders"] == (
        refresh_headers if refresh_headers is not None else {"x-fake-header": "foo"}
    )
    assert "alwaysStartWithDefaultModel" not in refreshed
    assert refreshed["inferenceModels"] == [{"name": "model-b"}]
    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        state_path=state_path,
    )
    assert reverted.changed is True
    assert not first.profile_path.exists()


def test_existing_same_name_is_not_adopted(tmp_path: Path, directory: Path) -> None:
    directory.mkdir()
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    old_id = _uuid()
    _write_json(directory / f"{old_id}.json", {"owner": "user"})
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["entries"].append({"id": old_id, "name": desktop.PROFILE_NAME})
    _write_json(directory / desktop.META_FILENAME, metadata)

    result = _configure(state_path)
    updated = _read(directory / desktop.META_FILENAME)

    assert result.profile_id != old_id
    assert {entry["id"] for entry in updated["entries"]} == {default_id, old_id, result.profile_id}
    assert _read(directory / f"{old_id}.json") == {"owner": "user"}


def test_malformed_metadata_and_manifest_fail_without_writes(
    tmp_path: Path, directory: Path
) -> None:
    directory.mkdir()
    state_path = tmp_path / "state.json"
    (directory / desktop.META_FILENAME).write_text("not json", encoding="utf-8")

    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(state_path)
    assert not state_path.exists()
    assert list(directory.iterdir()) == [directory / desktop.META_FILENAME]

    _write_json(directory / desktop.META_FILENAME, {"entries": [{"id": "bad", "name": "Default"}]})
    with pytest.raises(desktop.ClaudeDesktopConflictError):
        _configure(state_path)
    assert not state_path.exists()


@pytest.mark.parametrize("key", desktop.OWNED_PROFILE_KEYS)
@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_configure_refreshes_edited_and_deleted_owned_fields(
    tmp_path: Path,
    key: str,
    change: str,
    directory: Path,
) -> None:
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    default_path = directory / f"{default_id}.json"
    default_before = default_path.read_bytes()
    result = _configure(state_path)
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

    refreshed = _configure(state_path, models=("openai/gpt-oss-120b",))

    expected["inferenceModels"] = [{"name": "openai/gpt-oss-120b"}]
    assert refreshed.profile_id == result.profile_id
    assert refreshed.changed is True
    assert _read(result.profile_path) == expected
    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert default_path.read_bytes() == default_before
    record = next(iter(_read(state_path)["profiles"].values()))
    assert record["owned_after"] == {key: expected[key] for key in desktop.OWNED_PROFILE_KEYS}
    assert _configure(state_path, models=("openai/gpt-oss-120b",)).changed is False


def test_revert_after_refresh_removes_reapplied_owned_profile(
    tmp_path: Path, directory: Path
) -> None:
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    configured = _configure(state_path)
    profile = _read(configured.profile_path)
    profile["inferenceModels"] = [{"name": "user-model"}]
    _write_json(configured.profile_path, profile)

    _configure(state_path, models=("new-model",))
    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert reverted.changed is True
    assert reverted.drifted is False
    assert not configured.profile_path.exists()
    assert _read(configured.metadata_path) == {
        "appliedId": default_id,
        "entries": [{"id": default_id, "name": "Default"}],
    }
    assert _read(state_path)["profiles"] == {}


def test_metadata_failure_rolls_back_profile_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
) -> None:
    directory.mkdir()
    default_id = _seed_metadata(directory)
    state_path = tmp_path / "state.json"
    before_metadata = _read(directory / desktop.META_FILENAME)
    original_atomic_write = desktop_config.atomic_write_json
    failed = False

    def fail_metadata_once(path: Path, payload: object) -> None:
        nonlocal failed
        if Path(path) == directory / desktop.META_FILENAME and not failed:
            failed = True
            raise OSError("simulated metadata failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(desktop_config, "atomic_write_json", fail_metadata_once)
    with pytest.raises(desktop.ClaudeDesktopError, match="rolled back"):
        _configure(state_path)

    assert _read(directory / desktop.META_FILENAME) == before_metadata
    assert not state_path.exists()
    assert {path.name for path in directory.iterdir()} == {
        desktop.META_FILENAME,
        f"{default_id}.json",
    }
    assert _read(directory / desktop.META_FILENAME)["appliedId"] == default_id


def test_revert_restores_selection_and_keeps_user_profile_edits(
    tmp_path: Path, directory: Path
) -> None:
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    result = _configure(state_path)
    profile = _read(result.profile_path)
    profile["userOwned"] = {"keep": True}
    _write_json(result.profile_path, profile)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["metadataOwnedByUser"] = "keep"
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
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
    tmp_path: Path,
    reconfigure: bool,
    directory: Path,
) -> None:
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    result = _configure(state_path)
    if reconfigure:
        _configure(state_path, models=("new-model",))
    profile = _read(result.profile_path)
    profile["inferenceModels"] = [{"name": "user/model"}]
    _write_json(result.profile_path, profile)
    metadata = _read(directory / desktop.META_FILENAME)
    metadata["appliedId"] = default_id
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
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


def test_revert_preserves_user_renamed_profile_entry(tmp_path: Path, directory: Path) -> None:
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    result = _configure(state_path)
    metadata = _read(directory / desktop.META_FILENAME)
    next(entry for entry in metadata["entries"] if entry["id"] == result.profile_id)["name"] = (
        "User renamed"
    )
    _write_json(directory / desktop.META_FILENAME, metadata)

    reverted = desktop.revert_claude_desktop(
        "https://workspace.example",
        state_path=state_path,
    )

    assert reverted.changed is True
    updated = _read(directory / desktop.META_FILENAME)
    assert {entry["name"] for entry in updated["entries"]} == {"Default", "User renamed"}
    assert result.profile_path.exists()


def test_native_library_leaves_parent_metadata_untouched(tmp_path: Path, directory: Path) -> None:
    _seed_metadata(directory)
    parent_metadata = directory.parent / desktop.META_FILENAME
    _write_json(parent_metadata, {"owner": "user"})
    before = parent_metadata.read_bytes()
    state_path = tmp_path / "state.json"

    result = _configure(state_path)

    assert result.metadata_path == directory / desktop.META_FILENAME
    assert result.profile_path.parent == directory
    assert parent_metadata.read_bytes() == before


def test_existing_native_library_is_used_before_creating_metadata(
    tmp_path: Path, directory: Path
) -> None:
    directory.mkdir()

    result = _configure(tmp_path / "state.json")

    assert result.metadata_path == directory / desktop.META_FILENAME
    assert not (directory.parent / desktop.META_FILENAME).exists()


@pytest.fixture(params=["darwin", "win32", "linux"])
def native_directory(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    local_app_data = tmp_path / "relocated local app data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(desktop, "sys", SimpleNamespace(platform=request.param))
    monkeypatch.setattr(desktop, "_claude_desktop_installed", lambda: False)
    roots = {
        "darwin": home / "Library" / "Application Support" / "Claude-3p",
        "win32": local_app_data / "Claude-3p",
        "linux": home / ".config" / "Claude-3p",
    }
    return roots[request.param] / "configLibrary"


@pytest.fixture
def directory(native_directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    native_directory.parent.mkdir(parents=True)
    monkeypatch.setattr(desktop, "_claude_desktop_installed", lambda: True)
    return native_directory


def test_native_configure_refresh_and_revert_use_platform_profile_store(
    native_directory: Path, tmp_path: Path
) -> None:
    default_id = _seed_metadata(native_directory)
    before = (native_directory / f"{default_id}.json").read_bytes()
    state_path = tmp_path / "state.json"

    configured = _configure(state_path)
    assert configured.profile_path.parent == native_directory
    assert configured.metadata_path == native_directory / desktop.META_FILENAME
    assert _read(configured.metadata_path)["appliedId"] == configured.profile_id

    refreshed = _configure(state_path, models=("system.ai.claude-sonnet-5",))
    assert refreshed.profile_id == configured.profile_id
    assert _read(refreshed.profile_path)["inferenceModels"] == [
        {"name": "system.ai.claude-sonnet-5"}
    ]
    reverted = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)
    assert reverted.changed is True
    assert _read(configured.metadata_path)["appliedId"] == default_id
    assert (native_directory / f"{default_id}.json").read_bytes() == before
    assert not configured.profile_path.exists()


def test_native_resolution_does_not_install_missing_app(
    native_directory: Path, tmp_path: Path
) -> None:
    with pytest.raises(desktop.ClaudeDesktopUnavailableError, match="will not install"):
        _configure(tmp_path / "state.json")
    assert not native_directory.parent.exists()
    assert not (tmp_path / "state.json").exists()


def test_native_revert_without_ownership_does_not_create_files(
    native_directory: Path, tmp_path: Path
) -> None:
    state_path = tmp_path / "state.json"

    result = desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)

    assert result == desktop.RevertResult(profile_id=None, changed=False)
    assert not native_directory.parent.exists()
    assert not state_path.exists()


def test_native_resolution_initializes_library_after_app_has_opened(
    native_directory: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_directory.parent.mkdir(parents=True)
    monkeypatch.setattr(desktop, "_claude_desktop_installed", lambda: True)

    configured = _configure(tmp_path / "state.json")

    assert configured.profile_path.parent == native_directory
    assert _read(configured.metadata_path)["entries"] == [
        {"id": configured.profile_id, "name": desktop.PROFILE_NAME}
    ]


def test_native_library_file_is_rejected_without_modification(tmp_path, directory):
    directory.write_text("user-owned file", encoding="utf-8")
    state_path = tmp_path / "state.json"

    with pytest.raises(desktop.ClaudeDesktopUnavailableError, match="not a directory"):
        _configure(state_path)

    assert directory.read_text(encoding="utf-8") == "user-owned file"
    assert not state_path.exists()


def test_native_library_creation_failure_does_not_write_state(tmp_path, directory, monkeypatch):
    mkdir = Path.mkdir

    def deny_library_creation(path, *args, **kwargs):
        if path == directory:
            raise PermissionError("permission denied")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_library_creation)
    state_path = tmp_path / "state.json"

    with pytest.raises(desktop.ClaudeDesktopUnavailableError, match="Could not initialize"):
        _configure(state_path)

    assert not directory.exists()
    assert not state_path.exists()


def test_windows_native_path_defaults_to_local_app_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert desktop.claude_desktop_directory(home=tmp_path, platform="win32") == (
        tmp_path / "AppData" / "Local" / "Claude-3p" / "configLibrary"
    )


def test_unrecognized_native_platform_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop, "sys", SimpleNamespace(platform="freebsd"))
    with pytest.raises(desktop.ClaudeDesktopUnsupportedError, match="platform"):
        _configure(tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()


def test_fresh_metadata_has_no_dangling_default_and_revert_removes_it(
    tmp_path: Path, directory: Path
) -> None:
    state_path = tmp_path / "state.json"
    configured = _configure(state_path)
    metadata = _read(configured.metadata_path)
    for entry in metadata["entries"]:
        assert (directory / f"{entry['id']}.json").is_file()
    desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)
    assert not configured.profile_path.exists()
    assert not configured.metadata_path.exists()


def test_reverse_workspace_revert_does_not_restore_removed_profile(
    tmp_path: Path, directory: Path
) -> None:
    state_path = tmp_path / "state.json"
    _seed_metadata(directory)
    first = _configure(state_path, workspace="https://first.example")
    second = _configure(state_path, workspace="https://second.example")
    desktop.revert_claude_desktop("https://first.example", state_path=state_path)
    assert not first.profile_path.exists()
    desktop.revert_claude_desktop("https://second.example", state_path=state_path)
    metadata = _read(second.metadata_path)
    assert metadata.get("appliedId") != first.profile_id
    if metadata.get("appliedId"):
        assert (directory / f"{metadata['appliedId']}.json").is_file()


@pytest.mark.parametrize("failed_file", ["profile", "metadata", "manifest"])
@pytest.mark.parametrize("existing", ["absent", "unrecorded", "recorded"])
@pytest.mark.parametrize("operation", ["configure", "revert"])
def test_each_write_failure_preserves_existing_files(
    tmp_path,
    monkeypatch,
    failed_file,
    existing,
    operation,
    directory: Path,
):
    state_path = tmp_path / "state.json"
    default_id = _seed_metadata(directory)
    if existing == "unrecorded":
        profile_id = str(uuid.uuid5(desktop.PROFILE_NAMESPACE, "https://workspace.example"))
        _write_json(directory / f"{profile_id}.json", {"inferenceProvider": "old", "keep": True})
        metadata = _read(directory / desktop.META_FILENAME)
        metadata["entries"].append({"id": profile_id, "name": "Old label"})
        _write_json(directory / desktop.META_FILENAME, metadata)
    elif existing == "recorded":
        configured = _configure(state_path)
        profile = _read(configured.profile_path)
        profile["inferenceModels"] = [{"name": "user-model"}]
        if operation == "configure":
            profile["userOwned"] = {"keep": True}
        _write_json(configured.profile_path, profile)
        metadata = _read(configured.metadata_path)
        metadata["appliedId"] = default_id
        _write_json(configured.metadata_path, metadata)
    if operation == "revert":
        _configure(state_path, models=("new-model",))
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    state_before = state_path.read_bytes() if state_path.exists() else None
    write = desktop_config._write_or_remove
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

    monkeypatch.setattr(desktop_config, "_write_or_remove", fail_once)
    with pytest.raises(desktop.ClaudeDesktopError, match="rolled back"):
        if operation == "configure":
            _configure(state_path, models=("new-model",))
        else:
            desktop.revert_claude_desktop("https://workspace.example", state_path=state_path)
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    if state_before is None:
        assert not state_path.exists()
    else:
        assert state_path.read_bytes() == state_before
