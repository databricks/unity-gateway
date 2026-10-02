"""Component safety checks for the model-only workspace provisioning script."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cuj3_fixture_data import INVALID_DESTINATIONS, model_readback

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "fixtures/cuj/models/provision.py"


@pytest.fixture
def provisioner():
    return SimpleNamespace(**runpy.run_path(str(SCRIPT)))


def model_sources(provisioner):
    return {leaf: f"system.ai.{leaf}" for leaf in provisioner.MODEL_LEAVES}


class RecordingClient:
    def __init__(self, inventory):
        self.inventory = inventory
        self.requests = []

    def get_optional(self, path):
        self.requests.append(("GET", path, None, None))
        return self.inventory.get(path)

    def request(self, method, path, *, params=None, body=None):
        self.requests.append((method, path, params, body))
        if method == "GET":
            return self.inventory[path]
        if path.endswith("model-services"):
            schema = params["parent"].removeprefix("schemas/")
            self.inventory[f"{path}/{schema}.{params['model_service_id']}"] = body
        return body


def source_inventory(provisioner, sources=None):
    sources = model_sources(provisioner) if sources is None else sources
    return {
        f"{provisioner.UC}/models/{source}": {"full_name": source}
        for source in set(sources.values())
    }


def complete_inventory(provisioner):
    inventory = source_inventory(provisioner)
    inventory[f"{provisioner.UC}/catalogs/ug_e2e"] = {}
    inventory.update(
        {f"{provisioner.UC}/schemas/ug_e2e.{schema}": {} for schema in provisioner.SCHEMAS}
    )
    for leaf, source in model_sources(provisioner).items():
        schema = "other_models" if leaf.endswith("decoy") else "models"
        inventory[f"{provisioner.UC}/model-services/ug_e2e.{schema}.{leaf}"] = (
            provisioner.model_body(source)
        )
    return inventory


def test_model_fixture_create_body_uses_canonical_sdk_fields(provisioner):
    assert provisioner.model_body("system.ai.databricks-gpt-6-luna") == {
        "config": {
            "routing": {
                "destinations": [
                    {
                        "name": "system.ai.databricks-gpt-6-luna",
                        "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
                        "pay_per_token_config": {"model": "models/system.ai.databricks-gpt-6-luna"},
                        "traffic_percentage": 100,
                    }
                ]
            },
        }
    }


@pytest.mark.parametrize("server_defaults", [False, True])
def test_model_fixture_accepts_canonical_readback(provisioner, server_defaults):
    provisioner.validate_model(
        model_readback(server_defaults=server_defaults),
        "ug_e2e.models.gpt_luna",
        "system.ai.databricks-gpt-6-luna",
    )


@pytest.mark.parametrize("destination_updates", INVALID_DESTINATIONS)
def test_model_fixture_rejects_invalid_readback_before_writes(provisioner, destination_updates):
    inventory = complete_inventory(provisioner)
    inventory.pop(f"{provisioner.UC}/catalogs/ug_e2e")
    payload = model_readback(server_defaults=True)
    payload["config"]["routing"]["destinations"][0].update(destination_updates)
    inventory[f"{provisioner.UC}/model-services/ug_e2e.models.gpt_luna"] = payload
    sources = model_sources(provisioner)
    sources["gpt_luna"] = "system.ai.databricks-gpt-6-luna"
    inventory.update(source_inventory(provisioner, sources))
    client = RecordingClient(inventory)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        provisioner.provision(client, sources, apply=True)
    assert all(method == "GET" for method, *_ in client.requests)


@pytest.mark.parametrize("fallback", [False, True])
def test_model_fixture_rejects_multiple_sources_before_writes(provisioner, fallback):
    inventory = complete_inventory(provisioner)
    inventory.pop(f"{provisioner.UC}/catalogs/ug_e2e")
    payload = provisioner.model_body("system.ai.gpt_luna")
    routing = payload["config"]["routing"]
    extra = provisioner.model_body("system.ai.other")["config"]["routing"]["destinations"]
    if fallback:
        routing["fallback"] = {"destinations": extra}
    else:
        routing["destinations"].extend(extra)
    inventory[f"{provisioner.UC}/model-services/ug_e2e.models.gpt_luna"] = payload
    client = RecordingClient(inventory)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        provisioner.provision(client, model_sources(provisioner), apply=True)
    assert all(method == "GET" for method, *_ in client.requests)


def test_model_fixture_dry_plan_requires_no_credentials_or_network():
    arguments = [sys.executable, str(SCRIPT), "--workspace", "https://workspace.invalid"]
    for leaf in (
        "gpt-luna",
        "claude-haiku",
        "claude-sonnet",
        "kimi",
        "gemini-flash",
        "claude-decoy",
        "codex-decoy",
    ):
        arguments.extend([f"--{leaf}-source", f"system.ai.{leaf}"])
    environment = os.environ.copy()
    environment.pop("DATABRICKS_BEARER", None)
    result = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "no network or writes; no bearer required" in result.stdout
    assert result.stdout.count("model service ug_e2e.") == 7
    assert "model service ug_e2e.models.gemini_flash" in result.stdout
    assert "model service ug_e2e.other_models.claude_decoy" in result.stdout
    assert "model service ug_e2e.other_models.codex_decoy" in result.stdout
    assert "mcp-services" not in result.stdout
    assert "skills" not in result.stdout


def test_model_fixture_config_is_model_only():
    config = json.loads((ROOT / "fixtures/cuj-3/managed-config.json").read_text())
    assert "mcp_servers" not in config
    assert "skills" not in config
    agents = {entry["agent"]: entry["config"] for entry in config["enabled_agents"]}
    assert set(agents) == {"CODING_AGENT_CLAUDE_CODE", "CODING_AGENT_CODEX"}
    assert config["default_agent"] == "CODING_AGENT_CLAUDE_CODE"
    assert agents["CODING_AGENT_CLAUDE_CODE"]["default_models"] == {
        "default_model": "ug_e2e.models.claude_sonnet",
        "default_sonnet_model": "ug_e2e.models.claude_sonnet",
    }
    assert agents["CODING_AGENT_CODEX"]["default_models"] == {
        "default_model": "ug_e2e.models.gpt_luna"
    }
    for agent_config in agents.values():
        assert agent_config["models"] == {"unity_catalog_location": "ug_e2e.models"}
        assert agent_config["smart_routing"] == {"enabled": False}
        assert agent_config["tracing"] == {"enabled": False}


def test_model_fixture_validate_missing_inventory_performs_no_writes(provisioner):
    client = RecordingClient(source_inventory(provisioner))
    with pytest.raises(RuntimeError, match="Inventory is incomplete"):
        provisioner.provision(client, model_sources(provisioner), apply=False)
    assert len(client.requests) == 17
    assert all(method == "GET" for method, *_ in client.requests)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("registered_model", [None, {}, {"full_name": "system.ai.other"}, []])
def test_model_fixture_source_preflight_failure_performs_no_writes(
    provisioner, apply, registered_model
):
    sources = model_sources(provisioner)
    inventory = source_inventory(provisioner)
    source = sorted(sources.values())[-1]
    inventory[f"{provisioner.UC}/models/{source}"] = registered_model
    client = RecordingClient(inventory)
    with pytest.raises(RuntimeError, match="canonical registered-model FQN.*model-service alias"):
        provisioner.provision(client, sources, apply=apply)
    assert len(client.requests) == len(set(sources.values()))
    assert all(method == "GET" and "/models/" in path for method, path, *_ in client.requests)


def test_model_fixture_source_preflight_rejects_friendly_alias_without_resolving(provisioner):
    sources = model_sources(provisioner)
    inventory = source_inventory(provisioner)
    sources["gpt_luna"] = "system.ai.gpt-6-luna"
    inventory[f"{provisioner.UC}/models/system.ai.databricks-gpt-6-luna"] = {
        "full_name": "system.ai.databricks-gpt-6-luna"
    }
    inventory[f"{provisioner.UC}/model-services/system.ai.gpt-6-luna"] = model_readback(
        server_defaults=True
    )
    client = RecordingClient(inventory)
    with pytest.raises(RuntimeError, match="system.ai.gpt-6-luna.*model-service alias"):
        provisioner.provision(client, sources, apply=True)
    assert all(method == "GET" and "/models/" in path for method, path, *_ in client.requests)
    assert not any("databricks-gpt-6-luna" in path for _, path, *_ in client.requests)


def test_model_fixture_source_preflight_checks_each_unique_source_once(provisioner):
    sources = dict.fromkeys(provisioner.MODEL_LEAVES, "system.ai.databricks-gpt-6-luna")
    client = RecordingClient(source_inventory(provisioner, sources))
    provisioner.provision(client, sources, apply=True)
    source_reads = [request for request in client.requests if "/models/" in request[1]]
    assert source_reads == [
        ("GET", f"{provisioner.UC}/models/system.ai.databricks-gpt-6-luna", None, None)
    ]
    assert client.requests[0] == source_reads[0]


@pytest.mark.parametrize("apply", [False, True])
def test_model_fixture_existing_inventory_is_not_rewritten(provisioner, apply):
    inventory = complete_inventory(provisioner)
    for path, payload in inventory.items():
        if "/model-services/" in path:
            payload["config"]["routing"]["destinations"][0]["is_deleted"] = False
    client = RecordingClient(inventory)
    provisioner.provision(client, model_sources(provisioner), apply=apply)
    assert all(method == "GET" for method, *_ in client.requests)


def test_model_fixture_mismatch_aborts_before_any_creation(provisioner):
    inventory = complete_inventory(provisioner)
    inventory.pop(f"{provisioner.UC}/catalogs/ug_e2e")
    inventory[f"{provisioner.UC}/model-services/ug_e2e.other_models.codex_decoy"] = (
        provisioner.model_body("system.ai.wrong_model")
    )
    client = RecordingClient(inventory)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        provisioner.provision(client, model_sources(provisioner), apply=True)
    assert all(method == "GET" for method, *_ in client.requests)


def test_model_fixture_apply_only_creates_models_in_dependency_order(provisioner):
    client = RecordingClient(source_inventory(provisioner))
    provisioner.provision(client, model_sources(provisioner), apply=True)
    writes = [request for request in client.requests if request[0] != "GET"]
    assert [request[1] for request in writes] == [
        f"{provisioner.UC}/catalogs",
        *[f"{provisioner.UC}/schemas"] * 2,
        *[f"{provisioner.UC}/model-services"] * 7,
    ]
    assert all(request[0] == "POST" for request in writes)
    assert all(request[0] == "GET" for request in client.requests[:17])
    assert {request[1] for request in client.requests[:7]} == set(source_inventory(provisioner))
    model_writes = [request for request in writes if request[1].endswith("model-services")]
    expected_parents = {
        "gpt_luna": "schemas/ug_e2e.models",
        "claude_haiku": "schemas/ug_e2e.models",
        "claude_sonnet": "schemas/ug_e2e.models",
        "kimi": "schemas/ug_e2e.models",
        "gemini_flash": "schemas/ug_e2e.models",
        "claude_decoy": "schemas/ug_e2e.other_models",
        "codex_decoy": "schemas/ug_e2e.other_models",
    }
    assert {
        request[2]["model_service_id"]: request[2]["parent"] for request in model_writes
    } == expected_parents
    for request in model_writes:
        leaf = request[2]["model_service_id"]
        assert request[3] == provisioner.model_body(f"system.ai.{leaf}")


@pytest.mark.parametrize(
    "workspace",
    [
        "http://workspace.invalid",
        "https://user:secret@workspace.invalid",
        "https://workspace.invalid/path",
        "https://workspace.invalid/?token=secret",
    ],
)
def test_model_fixture_rejects_non_origin_workspaces(provisioner, workspace):
    with pytest.raises(ValueError, match="HTTPS workspace origin"):
        provisioner.workspace_origin(workspace)
