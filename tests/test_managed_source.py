"""Strict local wire input and immutable selected-source snapshots."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from ucode.managed_source import (
    load_file_config_manifest,
    read_file_source,
    validate_file_config,
)


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


@pytest.mark.parametrize("value", [True, 2, 1.0, "1"])
def test_spec_version_must_be_supported_int(value):
    """Non-int or newer version is rejected; mirrors _gate_config logic."""
    config = wire()
    config["spec_version"] = value
    with pytest.raises(RuntimeError, match="spec_version"):
        validate_file_config(config, "codex")


def test_missing_or_old_spec_versions_allowed():
    """Missing or older spec_version values are allowed (treated as v1)."""
    # Missing spec_version
    config = wire()
    del config["spec_version"]
    result = validate_file_config(config, "codex")
    assert "codex" in result["enabled_agents"]

    # spec_version 0 is allowed (old, but syntactically valid)
    config = wire()
    config["spec_version"] = 0
    result = validate_file_config(config, "codex")
    assert "codex" in result["enabled_agents"]


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


def test_unknown_config_fields_flow_through_normalization():
    """Unknown fields in config are accepted (normalization handles them)."""
    config = wire()
    config["enabled_agents"][0]["config"]["custom_env"] = {"KEY": "value"}
    result = validate_file_config(config, "codex")
    # The field flows through but is dropped by normalization if unknown
    assert "codex" in result["enabled_agents"]


@pytest.mark.parametrize("field", ["mcp_servers", "skills", "smart_defaults", "spend_tiers"])
def test_selectors_and_budgets_flow_through_to_manifest(field):
    """Selectors and budget tiers flow through normalization unchanged."""
    config = wire()
    if field in ("mcp_servers", "skills"):
        config[field] = {"names": ["main.schema.secret"]}
        expected = {"names": ["main.schema.secret"]}
    elif field == "smart_defaults":
        config[field] = {}
        expected = None  # Empty doesn't normalize to output
    else:  # spend_tiers
        config[field] = {"tiers": []}
        expected = None

    result = validate_file_config(config, "codex")
    if expected is not None:
        assert field in result
        assert result[field] == expected
    else:
        # Empty smart_defaults/spend_tiers normalize to None and are dropped
        assert field not in result


def test_multiple_agents_in_file():
    """Files can declare multiple agents; the requested one is verified to be present."""
    config = wire()
    config["enabled_agents"].extend(wire("claude")["enabled_agents"])
    result = validate_file_config(config, "codex")
    assert "codex" in result["enabled_agents"]
    assert "claude" in result["enabled_agents"]


def test_config_accepts_all_schema_fields():
    """Files accept the schema fields; normalization happens server-side."""
    config = wire()
    config["default_agent"] = "codex"
    entry = config["enabled_agents"][0]["config"]
    entry["smart_routing"] = {"enabled": True}
    entry["models"] = {"model_services": ["main.models.custom"]}

    # The file accepts this; normalization removes what the server doesn't need.
    result = validate_file_config(config, "codex")
    assert result["default_agent"] == "codex"
    assert result["enabled_agents"]["codex"]["smart_routing_enabled"]
    assert result["enabled_agents"]["codex"]["model_config"]["model_services"] == [
        "main.models.custom"
    ]


@pytest.mark.parametrize(
    "contents", [b"{", b"\xff", b'{"spec_version": 1, "spec_version": 1}', b'{"spec_version": NaN}']
)
def test_json_is_strict_and_redacted(tmp_path, contents):
    path = tmp_path / "policy.json"
    path.write_bytes(contents)
    with pytest.raises(RuntimeError, match="Invalid --config-file at"):
        read_file_source(str(path), "https://example.databricks.com", "codex")


def test_source_snapshot_is_immutable_and_ignores_metadata(tmp_path):
    config = wire()
    config.update({"workspace_id": "123", "update_time": "2099-01-01T00:00:00Z"})
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    first_path.write_text(json.dumps(config))
    second_path.write_bytes(first_path.read_bytes())
    first = read_file_source(str(first_path), "https://example.databricks.com", "codex")
    second = read_file_source(str(second_path), first.workspace, "codex")
    assert first.manifest == second.manifest
    assert "update_time" not in first.manifest
    view = first.manifest
    view["enabled_agents"].clear()
    assert "codex" in first.manifest["enabled_agents"]
    with pytest.raises(FrozenInstanceError):
        first.workspace = "https://other.example"
    config["handoff"] = {}
    with pytest.raises(RuntimeError, match="handoff"):
        validate_file_config(config, "codex")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_both_agents_can_omit_all_model_policy(tmp_path, agent):
    config = {
        "spec_version": 1,
        "enabled_agents": [
            {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {}},
            {"agent": "CODING_AGENT_CODEX", "config": {}},
        ],
    }
    path = tmp_path / "minimal.json"
    path.write_text(json.dumps(config))
    selected = read_file_source(str(path), "https://example.databricks.com", agent)
    assert selected.manifest["enabled_agents"] == {"claude": {}, "codex": {}}
    assert not selected.has_models


def test_configure_mode_accepts_file_without_specific_agent():
    """configure --config-file (agent=None) accepts a file without binding to a specific agent."""
    config = wire()
    result = validate_file_config(config, agent=None)
    assert "codex" in result["enabled_agents"]


def test_load_file_config_manifest(tmp_path):
    """load_file_config_manifest reads and validates a file without agent binding."""
    config = {
        "spec_version": 1,
        "enabled_agents": [
            {"agent": "CODING_AGENT_CLAUDE_CODE", "config": {}},
            {"agent": "CODING_AGENT_CODEX", "config": {}},
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    result = load_file_config_manifest(str(path))
    assert "claude" in result["enabled_agents"]
    assert "codex" in result["enabled_agents"]
