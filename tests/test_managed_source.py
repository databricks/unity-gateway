"""Strict local wire input and immutable selected-source snapshots."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from ucode.managed_source import read_file_source, validate_file_config


def wire(agent="codex", model="system.ai.gpt-5-6-sol"):
    return {
        "spec_version": 1,
        "enabled_agents": [
            {
                "agent": "CODING_AGENT_CODEX" if agent == "codex" else "CODING_AGENT_CLAUDE_CODE",
                "config": {"default_models": {"default_model": model}},
            }
        ],
    }


@pytest.mark.parametrize("value", [None, [], False, "secret-value", 1])
def test_requires_object(value):
    with pytest.raises(RuntimeError, match=r"at \$: expected an object") as error:
        validate_file_config(value, "codex")
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("value", [None, True, 0, -1, 2, 1.0, "1"])
def test_strict_supported_version(value):
    config = wire()
    config["spec_version"] = value
    with pytest.raises(RuntimeError, match="spec_version"):
        validate_file_config(config, "codex")


@pytest.mark.parametrize("value", [None, {}, [], "secret-value", [None]])
def test_enabled_agents_array(value):
    config = wire()
    config["enabled_agents"] = value
    with pytest.raises(RuntimeError, match="enabled_agents"):
        validate_file_config(config, "codex")


def test_duplicate_agent_aliases_and_requested_agent():
    config = wire()
    duplicate = copy.deepcopy(config["enabled_agents"][0])
    duplicate["agent"] = "codex"
    config["enabled_agents"].append(duplicate)
    with pytest.raises(RuntimeError, match=r"enabled_agents\[1\].agent: duplicate"):
        validate_file_config(config, "codex")
    with pytest.raises(RuntimeError, match="requested agent"):
        validate_file_config(wire(), "claude")
    config = wire()
    config["enabled_agents"][0]["agent"] = "future-agent-secret"
    with pytest.raises(RuntimeError, match="unknown agent") as error:
        validate_file_config(config, "codex")
    assert "future-agent-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value", "error_path"),
    [
        ("default_models", None, "default_models"),
        ("default_models", {"default_model": 12}, "default_model"),
        ("default_models", {"default_model": " "}, "default_model"),
        (
            "default_models",
            {"default_model": "ok", "default_opus_model": "x"},
            "default_opus_model",
        ),
        ("http_headers", {"X-Test": False}, "http_headers.X-Test"),
        ("http_headers", {" ": "private"}, "http_headers"),
        ("smart_routing", {"enabled": 1}, "smart_routing.enabled"),
        ("tracing", {"enabled": "true"}, "tracing.enabled"),
        ("tracing", None, "tracing"),
        ("models", {}, "models"),
        ("models", {"model_services": "a.b.c"}, "model_services"),
        ("models", {"model_services": ["bad"]}, "model_services"),
        ("models", {"model_services": ["a.b.c", "a.b.c"]}, "model_services"),
        ("models", {"model_services": [f"a.b.m{i}" for i in range(101)]}, "model_services"),
        ("models", {"unity_catalog_location": "a.b.c"}, "unity_catalog_location"),
        ("models", {"model_provider_service": " "}, "model_provider_service"),
        ("models", {"model_provider_service": "a.b.c", "unity_catalog_location": "a.b"}, "models"),
        ("models", {"model_services": ["a.b.c"]}, "default_models.default_model"),
        ("unknown", "private", "unknown"),
    ],
)
def test_rejects_raw_types_and_semantics_before_normalization(field, value, error_path):
    config = wire()
    config["enabled_agents"][0]["config"][field] = value
    with pytest.raises(RuntimeError) as error:
        validate_file_config(config, "codex")
    assert error_path in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("field", ["native_settings", "native_requirements"])
@pytest.mark.parametrize("value", [{}, None, {"secret": "private"}])
def test_unimplemented_extensions_never_silently_disappear(field, value):
    config = wire()
    config["enabled_agents"][0]["config"][field] = value
    with pytest.raises(RuntimeError, match=field):
        validate_file_config(config, "codex")


@pytest.mark.parametrize("field", ["skills", "mcp_servers", "smart_defaults", "spend_tiers"])
@pytest.mark.parametrize("value", [None, [], {"names": ["main.schema.secret"]}, {"tiers": []}])
def test_selector_and_budget_restrictions(field, value):
    config = wire()
    config[field] = value
    with pytest.raises(RuntimeError, match=field):
        validate_file_config(config, "codex")


def test_full_manifest_validation_includes_unrequested_agent():
    config = wire()
    config["enabled_agents"].extend(wire("claude")["enabled_agents"])
    config["enabled_agents"][1]["config"]["custom_env"] = {"INVALID": False}
    with pytest.raises(RuntimeError, match=r"enabled_agents\[1\].config.custom_env"):
        validate_file_config(config, "codex")


def test_defaults_and_routing_follow_published_contract():
    config = wire()
    assert "default_agent" not in validate_file_config(config, "codex")
    config["default_agent"] = "claude_code"
    with pytest.raises(RuntimeError, match="default_agent"):
        validate_file_config(config, "codex")
    config.pop("default_agent")
    entry = config["enabled_agents"][0]["config"]
    entry.pop("default_models")
    with pytest.raises(RuntimeError, match="default_models.default_model"):
        validate_file_config(config, "codex")
    entry["smart_routing"] = {"enabled": True}
    with pytest.raises(RuntimeError, match="static system.ai"):
        validate_file_config(config, "codex")
    entry["models"] = {"model_services": ["system.ai.gpt-5-6-sol"]}
    assert validate_file_config(config, "codex")["enabled_agents"]["codex"]["smart_routing_enabled"]


@pytest.mark.parametrize(
    "contents", [b"{", b"\xff", b'{"spec_version": 1, "spec_version": 1}', b'{"spec_version": NaN}']
)
def test_json_is_strict_and_redacted(tmp_path, contents):
    path = tmp_path / "policy.json"
    path.write_bytes(contents)
    with pytest.raises(RuntimeError, match="Invalid --config-file at"):
        read_file_source(str(path), "https://example.databricks.com", "codex")


def test_source_snapshot_identity_and_metadata(tmp_path):
    config = wire()
    config.update({"workspace_id": "123", "update_time": "2099-01-01T00:00:00Z"})
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    first_path.write_text(json.dumps(config))
    second_path.write_bytes(first_path.read_bytes())
    first = read_file_source(str(first_path), "https://example.databricks.com", "codex")
    second = read_file_source(str(second_path), first.workspace, "codex")
    assert first.digest == second.digest
    assert first.resolved_path != second.resolved_path
    assert "update_time" not in first.manifest
    view = first.manifest
    view["enabled_agents"].clear()
    assert "codex" in first.manifest["enabled_agents"]
    with pytest.raises(FrozenInstanceError):
        first.kind = "api"
    config["handoff"] = {}
    with pytest.raises(RuntimeError, match="handoff"):
        validate_file_config(config, "codex")
