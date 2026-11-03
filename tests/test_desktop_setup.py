"""Configure-only orchestration with real temporary Desktop profile files."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode import state as state_store
from ucode.agents.desktop import claude as claude_desktop
from ucode.agents.desktop import claude_config as desktop_config
from ucode.agents.desktop import setup as desktop_setup
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
            model_ids=[
                "system.ai.claude-haiku-4-5",
                "system.ai.oss-model",
                "anthropic-aigw-2b40afe0-system.ai.gpt-5-6-luna",
                "anthropic-aigw-12e8d1ee-system.ai.gpt-5-6-sol",
            ],
            model_id_to_display_name={},
        ),
    )
    return directory


def configured_profile(directory):
    metadata = json.loads((directory / "_meta.json").read_text())
    return json.loads((directory / f"{metadata['appliedId']}.json").read_text())


def test_configure_uses_claude_catalog_and_absolute_helper(desktop, tmp_path, capsys):
    desktop_setup.configure_claude_desktop(
        {"workspace": "https://workspace.example/", "profile": "chosen-profile"}
    )
    metadata = json.loads((desktop / "_meta.json").read_text())
    assert metadata["entries"] == [
        {"id": metadata["appliedId"], "name": "Unity Gateway - chosen-profile"}
    ]
    profile = configured_profile(desktop)
    assert profile["inferenceModels"] == [
        {"name": "system.ai.claude-haiku-4-5"},
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
    output = capsys.readouterr().out
    assert "gateway profile configured" in output
    assert "default-model" not in output


@pytest.mark.parametrize("source", ["discovery", "static"])
@pytest.mark.parametrize("tracing", [None, False, True])
def test_configure_otel_follows_claude_tracing_setting(desktop, source, tracing):
    state = {
        "workspace": "https://workspace.example/",
        "profile": "chosen-profile",
        "codex_otel_tracing": True,
    }
    if source == "static":
        state["claude_static_models"] = ["system.ai.claude-sonnet-5"]
    if tracing is not None:
        state["claude_otel_tracing"] = tracing
    original_state = dict(state)

    desktop_setup.configure_claude_desktop(state)

    profile = configured_profile(desktop)
    assert {key: value for key, value in profile.items() if key.startswith("otlp")} == (
        {
            "otlpEndpoint": "https://workspace.example/ai-gateway/otel",
            "otlpProtocol": "http/protobuf",
            "otlpHeadersHelper": str(
                claude_desktop.APP_DIR
                / (
                    "claude-desktop-otel-headers.cmd"
                    if desktop_setup.sys.platform == "win32"
                    else "claude-desktop-otel-headers.sh"
                )
            ),
            "otlpTracesEnabled": True,
        }
        if tracing
        else {}
    )
    assert "component-token" not in json.dumps(profile)
    helpers = list(claude_desktop.APP_DIR.glob("claude-desktop-otel-headers.*"))
    assert len(helpers) == int(bool(tracing))
    if tracing:
        helper = helpers[0].read_text()
        assert "otel-headers" in helper
        assert "https://workspace.example/" in helper
        assert "chosen-profile" in helper
        assert "component-token" not in helper
    assert state == original_state


def test_otel_reconfigure_switch_disable_and_revert_preserve_other_profiles(desktop):
    previous_id = "74074d15-5e9e-4465-a670-2d5bb1b83d00"
    previous = {"otlpEndpoint": "https://other-collector.example", "otlpTracesEnabled": True}
    previous_path = desktop / f"{previous_id}.json"
    previous_path.write_text(json.dumps(previous))
    metadata_path = desktop / "_meta.json"
    metadata = {
        "entries": [{"id": previous_id, "name": "Other profile"}],
        "appliedId": previous_id,
        "unrelated": "keep",
    }
    metadata_path.write_text(json.dumps(metadata))
    state = {
        "workspace": "https://workspace.example",
        "profile": "chosen-profile",
        "claude_otel_tracing": True,
    }
    desktop_setup.configure_claude_desktop(state)
    helper_path = Path(configured_profile(desktop)["otlpHeadersHelper"])
    helper_before = helper_path.read_bytes()
    before = {path.name: path.read_bytes() for path in desktop.glob("*.json")}
    desktop_setup.configure_claude_desktop(state)
    assert {path.name: path.read_bytes() for path in desktop.glob("*.json")} == before
    assert helper_path.read_bytes() == helper_before

    desktop_setup.configure_claude_desktop({**state, "workspace": "https://next.example/"})
    profile = configured_profile(desktop)
    assert profile["otlpEndpoint"] == "https://next.example/ai-gateway/otel"
    assert profile["inferenceGatewayBaseUrl"] == "https://next.example/ai-gateway/anthropic"
    assert "https://next.example/" in helper_path.read_text()

    desktop_setup.configure_claude_desktop({**state, "claude_otel_tracing": False})
    assert not any(key.startswith("otlp") for key in configured_profile(desktop))
    assert not helper_path.exists()
    desktop_setup.configure_claude_desktop(state)
    assert configured_profile(desktop)["otlpTracesEnabled"] is True
    assert desktop_setup.revert_desktop() == "removed"
    assert not (desktop / f"{claude_desktop.PROFILE_ID}.json").exists()
    assert not helper_path.exists()
    assert json.loads(previous_path.read_text()) == previous
    assert json.loads(metadata_path.read_text()) == metadata


@pytest.mark.parametrize("removed_directory", ["library", "app-root"])
def test_otel_revert_cleans_helper_after_desktop_removal(desktop, tmp_path, removed_directory):
    desktop_setup.configure_claude_desktop(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_otel_tracing": True,
        }
    )
    helper = Path(configured_profile(desktop)["otlpHeadersHelper"])
    state = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    original_directory = desktop if removed_directory == "library" else desktop.parent
    removed = original_directory.rename(tmp_path / "removed-desktop")
    before = {path: path.read_bytes() for path in removed.rglob("*.json")}

    assert desktop_setup.revert_desktop() == "removed"

    assert not helper.exists()
    assert not state.exists()
    assert not original_directory.exists()
    assert {path: path.read_bytes() for path in removed.rglob("*.json")} == before
    assert desktop_setup.revert_desktop() == "unchanged"
    assert not original_directory.exists()


@pytest.mark.parametrize("failed_file", ["helper", "state"])
def test_otel_revert_after_desktop_removal_rolls_back(desktop, tmp_path, monkeypatch, failed_file):
    desktop_setup.configure_claude_desktop(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_otel_tracing": True,
        }
    )
    helper = Path(configured_profile(desktop)["otlpHeadersHelper"])
    state = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    targets = {"helper": helper, "state": state}
    before = {path: path.read_bytes() for path in targets.values()}
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in targets.values()}
    desktop.rename(tmp_path / "removed-desktop")
    write = desktop_config._write_or_remove

    def fail_removal(path, contents, *, mode):
        if path == targets[failed_file] and contents is None:
            raise OSError("simulated removal failure")
        write(path, contents, mode=mode)

    monkeypatch.setattr(desktop_config, "_write_or_remove", fail_removal)

    assert desktop_setup.revert_desktop() == "failed"

    assert {path: path.read_bytes() for path in targets.values()} == before
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in targets.values()} == modes
    assert not desktop.exists()
    monkeypatch.setattr(desktop_config, "_write_or_remove", write)
    assert desktop_setup.revert_desktop() == "removed"
    assert not helper.exists()
    assert not state.exists()
    assert not desktop.exists()


@pytest.mark.parametrize("failed_file", ["helper", "profile", "metadata", "state"])
@pytest.mark.parametrize("operation", ["enable", "switch", "disable", "revert"])
def test_otel_write_failure_rolls_back_existing_profile(
    desktop, monkeypatch, failed_file, operation, capsys
):
    state = {
        "workspace": "https://workspace.example",
        "profile": "chosen-profile",
        "claude_otel_tracing": operation != "enable",
    }
    desktop_setup.configure_claude_desktop(state)
    metadata_path = desktop / "_meta.json"
    metadata = json.loads(metadata_path.read_text())
    previous_id = "74074d15-5e9e-4465-a670-2d5bb1b83d00"
    (desktop / f"{previous_id}.json").write_text("{}")
    metadata["entries"].append({"id": previous_id, "name": "Other profile"})
    metadata["appliedId"] = previous_id
    metadata_path.write_text(json.dumps(metadata))
    selection_path = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    targets = {
        "helper": claude_desktop.APP_DIR
        / (
            "claude-desktop-otel-headers.cmd"
            if desktop_setup.sys.platform == "win32"
            else "claude-desktop-otel-headers.sh"
        ),
        "profile": desktop / f"{claude_desktop.PROFILE_ID}.json",
        "metadata": metadata_path,
        "state": selection_path,
    }
    before = {path: path.read_bytes() if path.exists() else None for path in targets.values()}
    if operation == "switch":
        targets["helper"].chmod(0o600)
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in targets.values() if path.exists()}
    write = desktop_config._write_or_remove
    failed = False

    def fail_once(path, contents, *, mode):
        nonlocal failed
        if path == targets[failed_file] and not failed:
            failed = True
            raise OSError("simulated write failure")
        write(path, contents, mode=mode)

    monkeypatch.setattr(desktop_config, "_write_or_remove", fail_once)
    if operation == "revert":
        assert desktop_setup.revert_desktop() == "failed"
    else:
        desktop_setup.configure_claude_desktop(
            {
                **state,
                "workspace": "https://next.example"
                if operation == "switch"
                else state["workspace"],
                "claude_otel_tracing": operation != "disable",
            }
        )

    assert failed
    assert {
        path: path.read_bytes() if path.exists() else None for path in targets.values()
    } == before
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes
    assert "rolled back" in capsys.readouterr().out


def test_reconfigure_repairs_otel_helper_execute_permission(desktop):
    state = {
        "workspace": "https://workspace.example",
        "profile": "chosen-profile",
        "claude_otel_tracing": True,
    }
    desktop_setup.configure_claude_desktop(state)
    helper = Path(configured_profile(desktop)["otlpHeadersHelper"])
    original = helper.read_bytes()
    helper.chmod(0o600)
    desktop_setup.configure_claude_desktop(state)

    assert helper.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert helper.stat().st_mode & stat.S_IWUSR


@pytest.mark.parametrize("source", ["discovery", "static"])
def test_configure_filters_claude_names_without_changing_source(desktop, monkeypatch, source):
    claude_models = [
        "system.ai.claude-opus-5",
        "main.models.claude-sonnet-5-desktop",
        "databricks-claude-haiku-4-5",
        "anthropic/claude-fable-5",
        "claude-mythos-5",
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "claude-sonnet-4-5@20250929",
        "CLAUDE-OPUS-5",
        "system.ai.claude-opus-5",
    ]
    model_ids = [
        "anthropic-aigw-2b40afe0-system.ai.gpt-5-6-luna",
        *claude_models[:3],
        "anthropic-aigw-12e8d1ee-system.ai.gpt-5-6-sol",
        "system.ai.glm-5-3",
        "main.models.notclaude-opus-5",
        "main.models.claudecode",
        *claude_models[3:],
    ]
    original_ids = list(model_ids)
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    if source == "static":
        state["claude_static_models"] = model_ids
    else:
        monkeypatch.setattr(
            desktop_setup,
            "list_anthropic_model_catalog",
            lambda *args, **kwargs: AnthropicModelCatalog(model_ids, {}),
        )

    desktop_setup.configure_claude_desktop(state)

    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": model_id} for model_id in claude_models
    ]
    assert model_ids == original_ids


@pytest.mark.parametrize("source", ["discovery", "static"])
@pytest.mark.parametrize("existing", [False, True])
def test_no_claude_models_warns_without_changing_files(
    desktop, monkeypatch, capsys, source, existing
):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    if existing:
        desktop_setup.configure_claude_desktop(state)
    before = {path.name: path.read_bytes() for path in desktop.iterdir()}
    selection_path = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    selection_before = selection_path.read_bytes() if selection_path.exists() else None
    model_ids = [
        "anthropic-aigw-2b40afe0-system.ai.gpt-5-6-luna",
        "system.ai.glm-5-3",
    ]
    if source == "static":
        state["claude_static_models"] = model_ids
    else:
        monkeypatch.setattr(
            desktop_setup,
            "list_anthropic_model_catalog",
            lambda *args, **kwargs: AnthropicModelCatalog(model_ids, {}),
        )
    capsys.readouterr()

    desktop_setup.configure_claude_desktop(state)

    assert {path.name: path.read_bytes() for path in desktop.iterdir()} == before
    assert (selection_path.read_bytes() if selection_path.exists() else None) == selection_before
    output = capsys.readouterr().out
    assert "No Claude-family models found" in output
    assert "gateway profile configured" not in output


def test_reconfigure_refreshes_existing_profile_without_deletion(desktop, capsys, tmp_path):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
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

    desktop_setup.configure_claude_desktop(
        {
            **state,
            "profile": "updated-profile",
            "claude_static_models": ["main.models.claude-sonnet-5-desktop"],
            "claude_http_headers": {"X-Admin": "updated"},
        }
    )

    refreshed = configured_profile(desktop)
    original_metadata["entries"][0]["name"] = "Unity Gateway - updated-profile"
    assert json.loads(metadata_path.read_text()) == original_metadata
    assert refreshed["inferenceModels"] == [{"name": "main.models.claude-sonnet-5-desktop"}]
    assert refreshed["inferenceGatewayBaseUrl"] == "https://workspace.example/ai-gateway/anthropic"
    assert refreshed["inferenceCredentialHelper"] == str(tmp_path / "venv with spaces" / "python")
    assert refreshed["inferenceCredentialHelperArgs"][-2:] == ["--profile", "updated-profile"]
    assert refreshed["inferenceCustomHeaders"] == {
        "x-databricks-use-coding-agent-mode": "true",
        "X-Admin": "updated",
    }
    assert "userOwned" not in refreshed
    assert {path.name for path in desktop.glob("*.json")} == {
        "_meta.json",
        f"{profile_id}.json",
    }
    output = capsys.readouterr().out
    assert "gateway profile configured" in output
    assert "Could not configure Claude Desktop" not in output


def test_configure_overwrites_reserved_uuid_without_ownership_record(desktop, capsys):
    profile_id = claude_desktop.PROFILE_ID
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

    desktop_setup.configure_claude_desktop(state)

    metadata = json.loads((desktop / "_meta.json").read_text())
    assert metadata["entries"] == [{"id": profile_id, "name": "Unity Gateway - chosen-profile"}]
    assert metadata["userMetadata"] == "keep"
    assert configured_profile(desktop)["inferenceProvider"] == "gateway"
    assert "userOwned" not in configured_profile(desktop)
    assert "Could not configure Claude Desktop" not in capsys.readouterr().out

    assert desktop_setup.revert_desktop() == "removed"
    assert not profile_path.exists()
    assert json.loads((desktop / "_meta.json").read_text()) == {
        "entries": [],
        "userMetadata": "keep",
    }


def test_managed_static_catalog_uses_discovery_only_for_labels(desktop, monkeypatch):
    haiku = "main.models.claude-haiku-4-5"
    extra = "system.ai.claude-sonnet-5"
    calls = []

    def catalog(workspace, token, **kwargs):
        calls.append((workspace, token, kwargs))
        return AnthropicModelCatalog(
            model_ids=[extra, haiku],
            model_id_to_display_name={haiku: "Claude Haiku 4.5", extra: "Claude Sonnet 5"},
        )

    monkeypatch.setattr(desktop_setup, "list_anthropic_model_catalog", catalog)
    desktop_setup.configure_claude_desktop(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_static_models": [haiku, "main.models.gpt-5-6-sol"],
            "claude_http_headers": {"X-Admin": "configured"},
        }
    )
    assert calls == [
        (
            "https://workspace.example",
            "component-token",
            {"provider": None, "parent_schema": None},
        )
    ]
    profile = configured_profile(desktop)
    assert profile["inferenceModels"] == [
        {"name": haiku, "labelOverride": "Claude Haiku 4.5"},
    ]
    assert profile["inferenceCustomHeaders"]["X-Admin"] == "configured"


def test_managed_static_catalog_keeps_ids_when_label_discovery_fails(desktop, monkeypatch):
    haiku = "main.models.claude-haiku-4-5"
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(
            model_ids=[], model_id_to_display_name={}, error_msg="Discovery unavailable"
        ),
    )

    desktop_setup.configure_claude_desktop(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_static_models": [haiku],
        }
    )

    assert configured_profile(desktop)["inferenceModels"] == [{"name": haiku}]


def test_configure_refreshes_model_labels_from_discovery(desktop, monkeypatch):
    sonnet = "system.ai.claude-sonnet-5"
    opus = "system.ai.claude-opus-5"
    haiku = "system.ai.claude-haiku-4-5"
    gpt = "system.ai.gpt-5-6-sol"
    model_ids = [sonnet, gpt, opus, haiku, sonnet]
    display_names = {sonnet: "Claude Sonnet 5", opus: "", gpt: "GPT"}
    original_ids = list(model_ids)
    original_names = dict(display_names)
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(model_ids, display_names),
    )
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}

    desktop_setup.configure_claude_desktop(state)

    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": sonnet, "labelOverride": "Claude Sonnet 5"},
        {"name": opus},
        {"name": haiku},
        {"name": sonnet, "labelOverride": "Claude Sonnet 5"},
    ]
    assert model_ids == original_ids
    assert display_names == original_names

    display_names[sonnet] = "Sonnet — Updated label"
    display_names[opus] = "Claude Opus 5"
    desktop_setup.configure_claude_desktop(state)
    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": sonnet, "labelOverride": "Sonnet — Updated label"},
        {"name": opus, "labelOverride": "Claude Opus 5"},
        {"name": haiku},
        {"name": sonnet, "labelOverride": "Sonnet — Updated label"},
    ]

    del display_names[sonnet]
    display_names[opus] = ""
    desktop_setup.configure_claude_desktop(state)
    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": model_id} for model_id in [sonnet, opus, haiku, sonnet]
    ]
    assert model_ids == original_ids


@pytest.mark.parametrize("parent", [None, "main.models"])
def test_discovery_and_native_headers_use_same_source(desktop, monkeypatch, parent):
    calls = []

    def catalog(workspace, token, **kwargs):
        calls.append((workspace, token, kwargs))
        return AnthropicModelCatalog(
            model_ids=["us.anthropic.claude-sonnet-4-5-v1:0", "provider-model"],
            model_id_to_display_name={"us.anthropic.claude-sonnet-4-5-v1:0": "Claude Sonnet 4.5"},
        )

    monkeypatch.setattr(desktop_setup, "list_anthropic_model_catalog", catalog)
    desktop_setup.configure_claude_desktop(
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
    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": "us.anthropic.claude-sonnet-4-5-v1:0", "labelOverride": "Claude Sonnet 4.5"}
    ]


@pytest.mark.parametrize(
    "key,value",
    [
        ("custom_oauth", {"client_id": "custom"}),
        ("use_pat", True),
        ("profile", None),
    ],
)
def test_unsupported_auth_warns_without_modifying_profiles(desktop, capsys, key, value):
    before = (desktop / "_meta.json").read_bytes()
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile", key: value}
    desktop_setup.configure_claude_desktop(state)
    assert (desktop / "_meta.json").read_bytes() == before
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert "Could not configure Claude Desktop" in capsys.readouterr().out


def test_subscription_relay_uses_direct_gateway_and_combined_helper(desktop, monkeypatch):
    claude_binary = os.path.abspath(os.path.join("bin", "claude"))
    monkeypatch.setattr(desktop_setup.shutil, "which", lambda binary: claude_binary)
    state = {
        "workspace": "https://workspace.example",
        "profile": "chosen-profile",
        "claude_relayed": True,
        "provider_services": {"claude": "main.models.subscription"},
    }

    desktop_setup.configure_claude_desktop(state)

    profile = configured_profile(desktop)
    assert profile["inferenceGatewayBaseUrl"] == ("https://workspace.example/ai-gateway/anthropic")
    assert profile["inferenceCredentialHelperArgs"][-3:] == [
        "--relay",
        "--claude-binary",
        claude_binary,
    ]
    assert profile["inferenceCustomHeaders"][MODEL_PROVIDER_SERVICE_HEADER] == (
        "main.models.subscription"
    )


def test_subscription_relay_without_claude_code_preserves_profiles(desktop, monkeypatch, capsys):
    before = (desktop / "_meta.json").read_bytes()
    monkeypatch.setattr(desktop_setup.shutil, "which", lambda binary: None)

    desktop_setup.configure_claude_desktop(
        {
            "workspace": "https://workspace.example",
            "profile": "chosen-profile",
            "claude_relayed": True,
        }
    )

    assert (desktop / "_meta.json").read_bytes() == before
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert "Install Claude Code" in capsys.readouterr().out


def test_discovery_failure_is_warning_and_preserves_profile(desktop, monkeypatch, capsys):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
    before = {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    monkeypatch.setattr(
        desktop_setup,
        "list_anthropic_model_catalog",
        lambda *args, **kwargs: AnthropicModelCatalog(
            model_ids=[], model_id_to_display_name={}, error_msg="Discovery unavailable"
        ),
    )
    desktop_setup.configure_claude_desktop(state)
    assert before == {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    assert "Discovery unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["configure", "revert"])
def test_profile_lock_timeout_warns_and_preserves_state(desktop, monkeypatch, capsys, operation):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
    before = {path.name: path.read_bytes() for path in desktop.glob("*.json")}
    manifest_path = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    manifest_before = manifest_path.read_bytes()
    capsys.readouterr()

    def time_out(handle, *, timeout):
        assert timeout == 5
        raise TimeoutError("Lock is held by another operation")

    monkeypatch.setattr(desktop_config, "acquire_exclusive_file_lock", time_out)
    if operation == "configure":
        desktop_setup.configure_claude_desktop(state)
    else:
        assert desktop_setup.revert_desktop() == "failed"

    assert {path.name: path.read_bytes() for path in desktop.glob("*.json")} == before
    assert manifest_path.read_bytes() == manifest_before
    output = capsys.readouterr().out
    assert f"Could not {operation} Claude Desktop" in output
    assert "Timed out waiting" in output
    assert "retry" in output


def test_revert_failure_retains_workspace_state_and_allows_retry(desktop, monkeypatch):
    previous_id = "12345678-1234-4abc-8def-123456789abc"
    (desktop / f"{previous_id}.json").write_text("{}")
    metadata_path = desktop / "_meta.json"
    metadata_path.write_text(
        json.dumps({"entries": [{"id": previous_id, "name": "Existing"}], "appliedId": previous_id})
    )
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
    selection_path = claude_desktop.APP_DIR / claude_desktop.STATE_FILENAME
    selection_before = selection_path.read_bytes()
    profiles_before = {path.name: path.read_bytes() for path in desktop.glob("*.json")}
    monkeypatch.setattr(state_store, "APP_DIR", state_store.STATE_PATH.parent)
    state_store.save_state(state)

    # Other agents' cleanup must not touch host configuration.
    monkeypatch.setattr(cli, "restore_file", lambda *args: False)
    monkeypatch.setattr(cli, "revert_mcp_configs", lambda state: {})
    monkeypatch.setattr(cli, "revert_legacy_shared_config", lambda: False)
    monkeypatch.setattr(cli.claude_agent, "revert_managed_settings", lambda: "unchanged")
    monkeypatch.setattr(cli.codex_agent, "revert_managed_config", lambda: "unchanged")

    def lock_timeout(handle, *, timeout):
        raise TimeoutError("Lock is held by another operation")

    runner = CliRunner()
    with monkeypatch.context() as blocked:
        blocked.setattr(desktop_config, "acquire_exclusive_file_lock", lock_timeout)
        failed = runner.invoke(cli.app, ["revert"])

    assert failed.exit_code == 1, failed.output
    warnings = [line for line in failed.output.splitlines() if line.startswith("! ")]
    assert len(warnings) == 2, failed.output
    assert "Could not revert Claude Desktop" in warnings[0]
    assert (
        warnings[1] == "! ug state retained. Fix the Claude Desktop error and run ug revert again."
    )
    assert "Claude Desktop profile: failed" in failed.output

    assert state_store.load_state()["workspace"] == state["workspace"]
    assert state["workspace"] in state_store.load_full_state()["workspaces"]
    assert selection_path.read_bytes() == selection_before
    assert {path.name: path.read_bytes() for path in desktop.glob("*.json")} == profiles_before

    retried = runner.invoke(cli.app, ["revert"])
    assert retried.exit_code == 0, retried.output
    assert state_store.load_state() == {}
    assert not (desktop / f"{claude_desktop.PROFILE_ID}.json").exists()
    assert not selection_path.exists()
    assert json.loads(metadata_path.read_text()) == {
        "entries": [{"id": previous_id, "name": "Existing"}],
        "appliedId": previous_id,
    }
    assert (desktop / f"{previous_id}.json").read_text() == "{}"
    repeated = runner.invoke(cli.app, ["revert"])
    assert repeated.exit_code == 0, repeated.output


def test_dry_run_does_not_discover_or_create_profiles(desktop, monkeypatch):
    monkeypatch.setattr(desktop_setup, "is_dry_run", lambda: True)
    before = {p.name: p.read_bytes() for p in desktop.glob("*.json")}
    desktop_setup.configure_claude_desktop(
        {"workspace": "https://workspace.example", "profile": "chosen"}
    )
    assert before == {p.name: p.read_bytes() for p in desktop.glob("*.json")}


def test_unsupported_platform_does_not_configure_desktop(monkeypatch, capsys):
    monkeypatch.setattr(desktop_setup, "sys", SimpleNamespace(platform="freebsd"))
    desktop_setup.configure_claude_desktop({})
    assert capsys.readouterr().out == ""
    assert desktop_setup.revert_desktop() == "unchanged"


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
    desktop_setup.configure_claude_desktop({})
    assert "Open Claude Desktop once" in capsys.readouterr().out
    assert not directory.parent.exists()


def test_revert_removes_profile_and_selection_state(desktop):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
    assert desktop_setup.revert_desktop() == "removed"
    assert json.loads((desktop / "_meta.json").read_text()) == {"entries": []}
    assert list(desktop.glob("*.json")) == [desktop / "_meta.json"]
    assert desktop_setup.revert_desktop() == "unchanged"


def test_revert_without_desktop_ownership_leaves_existing_settings_untouched(desktop):
    before = {path.name: path.read_bytes() for path in desktop.iterdir()}

    assert desktop_setup.revert_desktop() == "unchanged"

    assert {path.name: path.read_bytes() for path in desktop.iterdir()} == before


def test_revert_cleans_cowork_when_native_desktop_is_unavailable(monkeypatch):
    monkeypatch.setattr(desktop_setup, "claude_desktop_directory", lambda: None)
    monkeypatch.setattr(desktop_setup, "revert_claude_cowork", lambda: "removed")

    assert desktop_setup.revert_desktop() == "removed"


def test_reconfigure_recreates_deleted_profile_with_fixed_id(desktop, capsys):
    state = {"workspace": "https://workspace.example", "profile": "chosen-profile"}
    desktop_setup.configure_claude_desktop(state)
    metadata_path = desktop / "_meta.json"
    original_metadata = json.loads(metadata_path.read_text())
    profile_id = original_metadata["appliedId"]
    profile_path = desktop / f"{profile_id}.json"
    profile_path.unlink()
    metadata_path.write_text(json.dumps({"entries": []}))
    capsys.readouterr()

    desktop_setup.configure_claude_desktop(state)

    assert json.loads(metadata_path.read_text()) == original_metadata
    assert configured_profile(desktop)["inferenceModels"] == [
        {"name": "system.ai.claude-haiku-4-5"},
    ]
    assert {path.name for path in desktop.glob("*.json")} == {
        "_meta.json",
        f"{profile_id}.json",
    }
    output = capsys.readouterr().out
    assert "gateway profile configured" in output
    assert "Could not configure Claude Desktop" not in output


def test_switching_workspace_replaces_one_reserved_profile(desktop):
    desktop_setup.configure_claude_desktop(
        {"workspace": "https://first.example", "profile": "first"}
    )
    desktop_setup.configure_claude_desktop(
        {"workspace": "https://second.example", "profile": "second"}
    )
    assert json.loads((desktop / "_meta.json").read_text()) == {
        "entries": [{"id": claude_desktop.PROFILE_ID, "name": "Unity Gateway - second"}],
        "appliedId": claude_desktop.PROFILE_ID,
    }
    profile = configured_profile(desktop)
    assert profile["inferenceGatewayBaseUrl"] == "https://second.example/ai-gateway/anthropic"
    assert profile["inferenceCredentialHelperArgs"][-4:] == [
        "--host",
        "https://second.example",
        "--profile",
        "second",
    ]
    assert {path.name for path in desktop.glob("*.json")} == {
        "_meta.json",
        f"{claude_desktop.PROFILE_ID}.json",
    }
    assert desktop_setup.revert_desktop() == "removed"
    assert not (desktop / f"{claude_desktop.PROFILE_ID}.json").exists()
