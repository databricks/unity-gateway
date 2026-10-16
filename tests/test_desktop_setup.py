"""Configure-only orchestration with real temporary Desktop profile files."""

import json
import uuid
from types import SimpleNamespace

import pytest

from ucode import desktop_setup
from ucode.agents.desktop import claude as claude_desktop
from ucode.agents.desktop import claude_config as desktop_config
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER, MODEL_SERVICE_PARENT_SCHEMA_HEADER
from ucode.databricks import AnthropicModelCatalog


@pytest.fixture(params=["darwin", "win32", "linux"])
def desktop(request, monkeypatch, tmp_path):
    home = tmp_path / "home"
    local_app_data = tmp_path / "relocated local app data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    roots = {
        "darwin": home / "Library" / "Application Support" / "Claude-3p",
        "win32": local_app_data / "Claude-3p",
        "linux": home / ".config" / "Claude-3p",
    }
    directory = roots[request.param] / "configLibrary"
    directory.mkdir(parents=True)
    (directory / "_meta.json").write_text(json.dumps({"entries": []}))
    monkeypatch.setattr(
        desktop_setup,
        "sys",
        SimpleNamespace(
            platform=request.param, executable=str(tmp_path / "venv with spaces" / "python")
        ),
    )
    monkeypatch.setattr(claude_desktop, "sys", SimpleNamespace(platform=request.param))
    monkeypatch.setattr(claude_desktop, "APP_DIR", tmp_path / "ug")
    monkeypatch.setattr(
        desktop_setup, "get_databricks_token", lambda host, profile: "component-token"
    )
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(
            model_ids=["system.ai.claude-haiku-4-5", "system.ai.oss-model"],
            model_id_to_display_name={},
        ),
    )
    return directory


def configured_profile(directory):
    metadata = json.loads((directory / "_meta.json").read_text())
    return json.loads((directory / f"{metadata['appliedId']}.json").read_text())


def test_configure_uses_full_gateway_catalog_and_absolute_helper(desktop, tmp_path):
    desktop_setup.configure_desktop_after_claude(
        {"workspace": "https://workspace.example/", "profile": "chosen-profile"}
    )
    metadata = json.loads((desktop / "_meta.json").read_text())
    assert metadata["entries"] == [
        {"id": metadata["appliedId"], "name": "Unity Gateway - chosen-profile"}
    ]
    profile = configured_profile(desktop)
    assert profile["inferenceModels"] == [
        {"name": "system.ai.claude-haiku-4-5"},
        {"name": "system.ai.oss-model"},
    ]
    assert profile["inferenceGatewayBaseUrl"] == "https://workspace.example/ai-gateway/anthropic"
    assert profile["inferenceCredentialHelper"] == str(tmp_path / "venv with spaces" / "python")
    assert profile["inferenceCredentialHelperArgs"] == [
        "-m",
        "ucode.cli",
        "claude-desktop-auth",
        "--host",
        "https://workspace.example/",
        "--profile",
        "chosen-profile",
    ]
    assert "component-token" not in json.dumps(profile)


def test_reconfigure_refreshes_existing_profile_without_deletion(desktop, capsys, tmp_path):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    metadata_path = desktop / "_meta.json"
    original_metadata = json.loads(metadata_path.read_text())
    profile_id = original_metadata["appliedId"]
    profile_path = desktop / f"{profile_id}.json"
    profile = configured_profile(desktop)
    profile["inferenceModels"] = [{"name": "user-model"}]
    profile["inferenceGatewayBaseUrl"] = "https://outdated.example"
    profile["inferenceCredentialHelper"] = "/old/install/ug"
    profile["inferenceCustomHeaders"] = {"X-Outdated": "remove"}
    profile["userOwned"] = {"keep": True}
    profile_path.write_text(json.dumps(profile))
    capsys.readouterr()

    desktop_setup.configure_desktop_after_claude(
        {
            **state,
            "profile": "updated-profile",
            "claude_static_models": ["main.models.new"],
            "claude_http_headers": {"X-Admin": "updated"},
        }
    )

    refreshed = configured_profile(desktop)
    original_metadata["entries"][0]["name"] = "Unity Gateway - updated-profile"
    assert json.loads(metadata_path.read_text()) == original_metadata
    assert refreshed["inferenceModels"] == [{"name": "main.models.new"}]
    assert refreshed["inferenceGatewayBaseUrl"] == "https://workspace.example/ai-gateway/anthropic"
    assert refreshed["inferenceCredentialHelper"] == str(tmp_path / "venv with spaces" / "python")
    assert refreshed["inferenceCredentialHelperArgs"][-2:] == ["--profile", "updated-profile"]
    assert refreshed["inferenceCustomHeaders"] == {
        "x-databricks-use-coding-agent-mode": "true",
        "X-Admin": "updated",
    }
    assert refreshed["userOwned"] == {"keep": True}
    assert {path.name for path in desktop.glob("*.json")} == {
        "_meta.json",
        f"{profile_id}.json",
    }
    output = capsys.readouterr().out
    assert "gateway profile configured" in output
    assert "Could not configure Claude Desktop" not in output


def test_configure_overwrites_workspace_uuid_without_ownership_record(desktop, capsys):
    profile_id = str(uuid.uuid5(claude_desktop.PROFILE_NAMESPACE, "https://workspace.example"))
    profile_path = desktop / f"{profile_id}.json"
    original_profile = {"inferenceProvider": "old-provider", "userOwned": {"keep": True}}
    profile_path.write_text(json.dumps(original_profile))
    original_metadata = {
        "appliedId": profile_id,
        "entries": [{"id": profile_id, "name": "Old label"}],
        "userMetadata": "keep",
    }
    (desktop / "_meta.json").write_text(json.dumps(original_metadata))
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}

    desktop_setup.configure_desktop_after_claude(state)

    metadata = json.loads((desktop / "_meta.json").read_text())
    assert metadata["entries"] == [{"id": profile_id, "name": "Unity Gateway - chosen-profile"}]
    assert metadata["userMetadata"] == "keep"
    assert configured_profile(desktop)["inferenceProvider"] == "gateway"
    assert configured_profile(desktop)["userOwned"] == {"keep": True}
    assert "Could not configure Claude Desktop" not in capsys.readouterr().out

    assert desktop_setup.revert_desktop(state) == "restored"
    assert json.loads(profile_path.read_text()) == original_profile
    assert json.loads((desktop / "_meta.json").read_text()) == original_metadata


def test_managed_static_catalog_does_not_fetch_discovery(desktop, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A managed static catalog must not trigger discovery")

    monkeypatch.setattr(desktop_setup, "list_anthropic_model_catalog", forbidden)
    desktop_setup.configure_desktop_after_claude(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_static_models": ["main.models.cheap", "main.models.oss"],
            "claude_http_headers": {"X-Admin": "configured"},
        }
    )
    profile = configured_profile(desktop)
    assert profile["inferenceModels"] == [
        {"name": "main.models.cheap"},
        {"name": "main.models.oss"},
    ]
    assert profile["inferenceCustomHeaders"]["X-Admin"] == "configured"


@pytest.mark.parametrize("parent", [None, "main.models"])
def test_discovery_and_native_headers_use_same_source(desktop, monkeypatch, parent):
    calls = []

    def catalog(workspace, token, **kwargs):
        calls.append((workspace, token, kwargs))
        return AnthropicModelCatalog(model_ids=["provider-model"], model_id_to_display_name={})

    monkeypatch.setattr(desktop_setup, "list_anthropic_model_catalog", catalog)
    desktop_setup.configure_desktop_after_claude(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "provider_services": {"claude": "main.models.provider"},
            "claude_static_models": ["ignored-static-model"],
        },
        parent_schema=parent,
    )
    expected_provider = None if parent else "main.models.provider"
    assert calls == [
        (
            "https://workspace.example",
            "component-token",
            {"provider": expected_provider, "parent_schema": parent},
        )
    ]
    headers = configured_profile(desktop)["inferenceCustomHeaders"]
    if parent:
        assert headers[MODEL_SERVICE_PARENT_SCHEMA_HEADER] == parent
        assert MODEL_PROVIDER_SERVICE_HEADER not in headers
    else:
        assert headers[MODEL_PROVIDER_SERVICE_HEADER] == expected_provider
    assert configured_profile(desktop)["inferenceModels"] == [{"name": "provider-model"}]


@pytest.mark.parametrize(
    "key,value",
    [
        ("claude_relayed", True),
        ("custom_oauth", {"client_id": "custom"}),
        ("use_pat", True),
        ("profile", None),
    ],
)
def test_unsupported_auth_warns_without_modifying_profiles(desktop, capsys, key, value):
    before = (desktop / "_meta.json").read_bytes()
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile", key: value}
    desktop_setup.configure_desktop_after_claude(state)
    assert (desktop / "_meta.json").read_bytes() == before
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert "Could not configure Claude Desktop" in capsys.readouterr().out


def test_discovery_failure_is_warning_and_preserves_profile(desktop, monkeypatch, capsys):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    before = {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(
            model_ids=[], model_id_to_display_name={}, error_msg="Discovery unavailable"
        ),
    )
    desktop_setup.configure_desktop_after_claude(state)
    assert before == {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    assert "Discovery unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["configure", "revert"])
def test_profile_lock_timeout_warns_and_preserves_state(desktop, monkeypatch, capsys, operation):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    before = {path.name: path.read_bytes() for path in desktop.glob("*.json")}
    manifest_path = claude_desktop.APP_DIR / claude_desktop.MANIFEST_FILENAME
    manifest_before = manifest_path.read_bytes()
    capsys.readouterr()

    def time_out(handle, *, timeout):
        assert timeout == 5
        raise TimeoutError("Lock is held by another operation")

    monkeypatch.setattr(desktop_config, "acquire_exclusive_file_lock", time_out)
    if operation == "configure":
        desktop_setup.configure_desktop_after_claude(state)
    else:
        assert desktop_setup.revert_desktop(state) == "failed"

    assert {path.name: path.read_bytes() for path in desktop.glob("*.json")} == before
    assert manifest_path.read_bytes() == manifest_before
    output = capsys.readouterr().out
    assert f"Could not {operation} Claude Desktop" in output
    assert "Timed out waiting" in output
    assert "retry" in output


def test_dry_run_does_not_discover_or_create_profiles(desktop, monkeypatch):
    monkeypatch.setattr(desktop_setup, "is_dry_run", lambda: True)
    before = {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    desktop_setup.configure_desktop_after_claude(
        {"workspace": "https://workspace.example", "profile": "chosen"}
    )
    assert before == {p.name: p.read_bytes() for p in desktop.glob("*.json")}


def test_unsupported_platform_does_not_configure_desktop(monkeypatch, capsys):
    monkeypatch.setattr(desktop_setup, "sys", SimpleNamespace(platform="freebsd"))
    desktop_setup.configure_desktop_after_claude({})
    assert capsys.readouterr().out == ""
    assert desktop_setup.revert_desktop({}) == "unchanged"


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_missing_app_warns_before_model_discovery(platform, monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    local_app_data = tmp_path / "local app data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    root = local_app_data / "Claude-3p" if platform == "win32" else home / ".config" / "Claude-3p"
    directory = root / "configLibrary"
    monkeypatch.setattr(desktop_setup, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(claude_desktop, "sys", SimpleNamespace(platform=platform))

    def reject_token_fetch(*_args):
        pytest.fail("Missing Desktop must be detected before external authentication")

    monkeypatch.setattr(desktop_setup, "get_databricks_token", reject_token_fetch)
    desktop_setup.configure_desktop_after_claude({})
    assert "Open Claude Desktop once" in capsys.readouterr().out
    assert not directory.parent.exists()


def test_revert_uses_independent_profile_manifest(desktop):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    assert desktop_setup.revert_desktop(state) == "restored"
    assert json.loads((desktop / "_meta.json").read_text()) == {"entries": []}
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert desktop_setup.revert_desktop(state) == "unchanged"


def test_revert_without_desktop_ownership_leaves_existing_settings_untouched(desktop):
    before = {path.name: path.read_bytes() for path in desktop.iterdir()}

    assert (
        desktop_setup.revert_desktop(
            {"workspace": "https://workspace.example", "profile": "chosen-profile"}
        )
        == "unchanged"
    )

    assert {path.name: path.read_bytes() for path in desktop.iterdir()} == before


def test_reconfigure_recreates_deleted_profile_with_same_workspace_id(desktop, capsys):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    metadata_path = desktop / "_meta.json"
    original_metadata = json.loads(metadata_path.read_text())
    profile_id = original_metadata["appliedId"]
    profile_path = desktop / f"{profile_id}.json"
    profile_path.unlink()
    metadata_path.write_text(json.dumps({"entries": []}))
    capsys.readouterr()

    desktop_setup.configure_desktop_after_claude(state)

    assert json.loads(metadata_path.read_text()) == original_metadata
    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": "system.ai.claude-haiku-4-5"},
        {"name": "system.ai.oss-model"},
    ]
    assert {path.name for path in desktop.glob("*.json")} == {
        "_meta.json",
        f"{profile_id}.json",
    }
    output = capsys.readouterr().out
    assert "gateway profile configured" in output
    assert "Could not configure Claude Desktop" not in output
