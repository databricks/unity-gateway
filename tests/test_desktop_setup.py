"""Configure-only orchestration with real temporary Desktop profile files."""

import json
from types import SimpleNamespace

import pytest

from ucode import desktop_setup
from ucode.agents import claude_desktop
from ucode.constants import MODEL_PROVIDER_SERVICE_HEADER, MODEL_SERVICE_PARENT_SCHEMA_HEADER
from ucode.databricks import AnthropicModelCatalog


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    directory = tmp_path / "Claude-3p" / "configLibrary"
    directory.mkdir(parents=True)
    (directory / "_meta.json").write_text(json.dumps({"entries": []}))
    monkeypatch.setattr(
        desktop_setup,
        "sys",
        SimpleNamespace(platform="darwin", executable="/venv with spaces/bin/python"),
    )
    monkeypatch.setattr(desktop_setup, "claude_desktop_directory", lambda: directory)
    monkeypatch.setattr(claude_desktop, "claude_desktop_directory", lambda: directory)
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


def test_configure_uses_full_gateway_catalog_and_absolute_helper(desktop):
    desktop_setup.configure_desktop_after_claude(
        {"workspace": "https://workspace.example/", "profile": "chosen-profile"}
    )
    profile = configured_profile(desktop)
    assert profile["inferenceModels"] == [
        {"name": "system.ai.claude-haiku-4-5"},
        {"name": "system.ai.oss-model"},
    ]
    assert profile["inferenceGatewayBaseUrl"] == "https://workspace.example/ai-gateway/anthropic"
    assert profile["inferenceCredentialHelper"] == "/venv with spaces/bin/python"
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


def test_dry_run_does_not_discover_or_create_profiles(desktop, monkeypatch):
    monkeypatch.setattr(desktop_setup, "is_dry_run", lambda: True)
    before = {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    desktop_setup.configure_desktop_after_claude(
        {"workspace": "https://workspace.example", "profile": "chosen"}
    )
    assert before == {p.name: p.read_bytes() for p in desktop.glob("*.json")}


def test_linux_noop_and_windows_unverified_warning(monkeypatch, capsys):
    monkeypatch.setattr(desktop_setup, "sys", SimpleNamespace(platform="linux"))
    desktop_setup.configure_desktop_after_claude({})
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(desktop_setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(desktop_setup, "claude_desktop_directory", lambda: None)
    desktop_setup.configure_desktop_after_claude({})
    assert "Windows profile location" in capsys.readouterr().out


def test_revert_uses_independent_profile_manifest(desktop):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_desktop_after_claude(state)
    assert desktop_setup.revert_desktop(state) == "restored"
    assert json.loads((desktop / "_meta.json").read_text()) == {"entries": []}
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert desktop_setup.revert_desktop(state) == "unchanged"
