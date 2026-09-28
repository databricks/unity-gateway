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
    config["enabled_agents"][1]["config"]["custom_env"] = {"FEATURE_FLAG": False}
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
    assert validate_file_config(config, "codex")["enabled_agents"]["codex"] == {}
    entry["smart_routing"] = {"enabled": True}
    assert validate_file_config(config, "codex")["enabled_agents"]["codex"]["smart_routing_enabled"]
    entry["models"] = {"model_services": ["main.models.custom"]}
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


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_custom_env_is_agent_scoped_exact_and_separate_from_api_normalization(tmp_path, agent):
    from ucode.managed_config import normalize_managed_config

    custom_env = {
        "OTEL_RESOURCE_ATTRIBUTES": "label=a,b=c\nsecond=line",
        "EMPTY": "",
        "FLAG": " 1 ",
    }
    config = {
        "spec_version": 1,
        "enabled_agents": [
            {"agent": "claude_code", "config": {"custom_env": custom_env}},
            {"agent": "codex", "config": {"custom_env": {"FLAG": "codex only"}}},
        ],
    }
    path = tmp_path / "env.json"
    path.write_text(json.dumps(config))
    source = read_file_source(str(path), "https://example.databricks.com", agent)
    assert source.custom_env == (custom_env if agent == "claude" else {"FLAG": "codex only"})
    assert not source.has_models
    view = source.custom_env
    view.clear()
    assert source.custom_env
    assert "codex only" not in repr(source)
    assert normalize_managed_config(config)["enabled_agents"] == {"claude": {}, "codex": {}}


@pytest.mark.parametrize(
    "value", [None, [], True, "private", {"FLAG": False}, {"FLAG": 1}, {"FLAG": "private\0value"}]
)
def test_custom_env_rejects_invalid_maps_and_values(value):
    config = wire()
    config["enabled_agents"][0]["config"]["custom_env"] = value
    with pytest.raises(RuntimeError, match="custom_env") as error:
        validate_file_config(config, "codex")
    assert "private" not in str(error.value)


@pytest.mark.parametrize("name", ["", "1FLAG", "BAD-NAME", "A=B", "ENV\nNAME", "é", "NAME\0"])
def test_custom_env_requires_portable_names(name):
    config = wire()
    config["enabled_agents"][0]["config"]["custom_env"] = {name: "private"}
    with pytest.raises(RuntimeError, match="portable environment variable names") as error:
        validate_file_config(config, "codex")
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "Home",
        "XDG_CONFIG_HOME",
        "Databricks_HOST",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
        "CLAUDE_CODE_HOST_CREDS_FILE",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_OAUTH_CLIENT_ID",
        "CLAUDE_CODE_CUSTOM_OAUTH_URL",
        "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
        "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "OAUTH_TOKEN",
        "UG_OPTION",
        "UCODE_DEBUG",
        "ENABLE_SMART_ROUTING_V2",
        "SMART_ROUTER_NAME",
        "CLAUDE_CODE_USE_BEDROCK",
        "NODE_OPTIONS",
    ],
)
def test_custom_env_reserves_auth_path_config_and_routing_names(name):
    config = wire()
    config["enabled_agents"][0]["config"]["custom_env"] = {name: "private"}
    with pytest.raises(RuntimeError, match="reserved") as error:
        validate_file_config(config, "codex")
    assert "private" not in str(error.value)


def test_custom_env_rejects_case_insensitive_duplicates():
    config = wire()
    config["enabled_agents"][0]["config"]["custom_env"] = {"FLAG": "first", "flag": "second"}
    with pytest.raises(RuntimeError, match="duplicate environment name ignoring case"):
        validate_file_config(config, "codex")


@pytest.mark.parametrize("agent", ["gemini", "opencode", "copilot", "pi"])
def test_custom_env_rejected_on_other_agents_even_when_not_requested(agent):
    config = wire()
    config["enabled_agents"].append({"agent": agent, "config": {"custom_env": {}}})
    with pytest.raises(RuntimeError, match="only Claude and Codex"):
        validate_file_config(config, "codex")
