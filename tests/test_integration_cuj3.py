"""Component checks for CUJ3 discovery helpers and native model evidence."""

import io
import json
import runpy
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.utils import cuj3

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "fixtures/cuj/models/provision.py"


@pytest.fixture
def provisioner():
    return SimpleNamespace(**runpy.run_path(str(SCRIPT)))


def model_readback(*, server_defaults):
    destination = {
        "name": "system.ai.databricks-gpt-6-luna",
        "destination_type": "DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL",
        "pay_per_token_config": {"model": "models/system.ai.databricks-gpt-6-luna"},
    }
    routing = {"destinations": [destination]}
    if server_defaults:
        destination.update(traffic_percentage=100, is_deleted=False)
        routing["fallback"] = {"destinations": []}
    return {
        "name": "model-services/ug_e2e.models.gpt_luna",
        "config": {"routing": routing, "usage_tracking": {"enabled": True}},
    }


INVALID_DESTINATIONS = [
    {"name": "system.ai.wrong_source"},
    {"name": "other.ai.fixture"},
    {"destination_type": "DESTINATION_TYPE_EXTERNAL_MODEL"},
    {"pay_per_token_config": {"model": "models/system.ai.wrong_source"}},
    {"pay_per_token_config": None},
    {"traffic_percentage": 0},
    {"traffic_percentage": 50},
    {"traffic_percentage": "100"},
    {"traffic_percentage": None},
    {"traffic_percentage": True},
    {"is_deleted": True},
    {"is_deleted": "false"},
    {"is_disabled": True},
    {"disabled": True},
]


@pytest.mark.parametrize(
    ("wire_id", "expected"),
    [
        ("ug_e2e.models.kimi", "ug_e2e.models.kimi"),
        ("anthropic-aigw-7bf7897e-ug_e2e.models.kimi", "ug_e2e.models.kimi"),
        ("ug_e2e.models.claude_sonnet", "ug_e2e.models.claude_sonnet"),
        (
            "anthropic-aigw-00000000-ug_e2e.models.kimi",
            "anthropic-aigw-00000000-ug_e2e.models.kimi",
        ),
        (
            "anthropic-aigw-7bf7897e-ug_e2e.models.kimi_v2",
            "anthropic-aigw-7bf7897e-ug_e2e.models.kimi_v2",
        ),
        (
            "anthropic-aigw-7bf7897e-ug_e2e.other_models.kimi",
            "anthropic-aigw-7bf7897e-ug_e2e.other_models.kimi",
        ),
        (
            "anthropic-aigw-7BF7897E-ug_e2e.models.kimi",
            "anthropic-aigw-7BF7897E-ug_e2e.models.kimi",
        ),
    ],
)
def test_claude_service_identity_requires_exact_gateway_alias_checksum(wire_id, expected):
    assert cuj3.claude_model_service_id(wire_id) == expected


def test_claude_discovery_requires_exact_gateway_wire_ids():
    assert {cuj3.claude_discovery_model_id(model) for model in cuj3.CLAUDE_MODELS} == {
        "ug_e2e.models.claude_haiku",
        "ug_e2e.models.claude_sonnet",
        "anthropic-aigw-7bf7897e-ug_e2e.models.kimi",
    }
    assert cuj3.claude_discovery_model_id(cuj3.CLAUDE_DECOY) == cuj3.CLAUDE_DECOY


def test_model_discovery_matrix_filters_families_without_changing_schema_scope(provisioner):
    expected_models = {
        "ug_e2e.models.gpt_luna",
        "ug_e2e.models.claude_haiku",
        "ug_e2e.models.claude_sonnet",
        "ug_e2e.models.kimi",
        "ug_e2e.models.gemini_flash",
    }
    assert cuj3.MODEL_SERVICES == expected_models
    assert cuj3.CLAUDE_MODELS == {
        "ug_e2e.models.claude_haiku",
        "ug_e2e.models.claude_sonnet",
        "ug_e2e.models.kimi",
    }
    assert cuj3.CODEX_MODELS == {"ug_e2e.models.gpt_luna", "ug_e2e.models.kimi"}
    assert cuj3.GEMINI_MODEL in cuj3.MODEL_SERVICES
    assert cuj3.GEMINI_MODEL not in cuj3.CLAUDE_MODELS | cuj3.CODEX_MODELS
    assert cuj3.CLAUDE_DEFAULT in cuj3.CLAUDE_MODELS
    assert cuj3.CODEX_DEFAULT in cuj3.CODEX_MODELS
    assert cuj3.CLAUDE_DECOY not in cuj3.MODEL_SERVICES
    assert cuj3.CODEX_DECOY not in cuj3.MODEL_SERVICES
    assert {
        f"ug_e2e.models.{leaf}" for leaf in provisioner.MODEL_LEAVES if not leaf.endswith("decoy")
    } == expected_models


@pytest.mark.parametrize("server_defaults", [False, True])
def test_live_inventory_proves_gemini_exists_without_using_agent_discovery(
    monkeypatch, server_defaults
):
    requests = []
    handlers = []

    class InventoryOpener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(model_readback(server_defaults=server_defaults)).encode())

    def build_opener(handler):
        handlers.append(handler)
        return InventoryOpener()

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", build_opener)
    inventory = cuj3.fetch_model_service_inventory("https://workspace.invalid/", "test-bearer")
    expected = cuj3.MODEL_SERVICES | {cuj3.CLAUDE_DECOY, cuj3.CODEX_DECOY}
    assert set(inventory) == expected
    assert inventory[cuj3.GEMINI_MODEL] == ("system.ai.databricks-gpt-6-luna",)
    assert len(requests) == len(expected)
    assert len(handlers) == 1 and isinstance(handlers[0], cuj3.NoRedirect)
    for request, timeout in requests:
        assert request.get_method() == "GET"
        assert request.full_url.startswith(
            "https://workspace.invalid/api/2.1/unity-catalog/model-services/"
        )
        assert request.data is None
        assert request.get_header("Authorization") == "Bearer test-bearer"
        assert timeout == 30


@pytest.mark.parametrize("destination_updates", INVALID_DESTINATIONS)
def test_live_inventory_rejects_invalid_source_routing(monkeypatch, destination_updates):
    payload = model_readback(server_defaults=True)
    payload["config"]["routing"]["destinations"][0].update(destination_updates)

    class InvalidOpener:
        def open(self, request, *, timeout):
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: InvalidOpener())
    with pytest.raises(AssertionError):
        cuj3.fetch_model_service_inventory("https://workspace.invalid", "test-bearer")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"config": {}},
        {"config": {"routing": {"destinations": []}}},
        {"config": {"routing": {"destinations": [None]}}},
        {"config": {"routing": {"destinations": [{"name": "system.ai.fixture"}]}}},
    ],
)
def test_live_inventory_rejects_missing_or_malformed_services(monkeypatch, payload):
    class InvalidOpener:
        def open(self, request, *, timeout):
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: InvalidOpener())
    with pytest.raises(AssertionError):
        cuj3.fetch_model_service_inventory("https://workspace.invalid", "test-bearer")


def test_live_inventory_fails_on_missing_gemini_even_when_other_models_exist(
    provisioner, monkeypatch
):
    class MissingGeminiOpener:
        def open(self, request, *, timeout):
            if request.full_url.endswith(cuj3.GEMINI_MODEL):
                raise urllib.error.HTTPError(request.full_url, 404, "Not found", {}, None)
            return io.BytesIO(json.dumps(provisioner.model_body("system.ai.fixture")).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: MissingGeminiOpener())
    with pytest.raises(AssertionError, match=r"gemini_flash.*HTTP 404"):
        cuj3.fetch_model_service_inventory("https://workspace.invalid", "test-bearer")


@pytest.mark.parametrize("fallback", [False, True])
def test_live_inventory_rejects_multiple_backing_models(provisioner, monkeypatch, fallback):
    payload = provisioner.model_body("system.ai.fixture")
    routing = payload["config"]["routing"]
    if fallback:
        routing["fallback"] = {"destinations": routing["destinations"]}
    else:
        routing["destinations"] *= 2

    class MultipleSourceOpener:
        def open(self, request, *, timeout):
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: MultipleSourceOpener())
    with pytest.raises(AssertionError):
        cuj3.fetch_model_service_inventory("https://workspace.invalid", "test-bearer")


@pytest.mark.parametrize("cursor", ["", "❯ ", "› ", "> "])
@pytest.mark.parametrize("label", ["ug_e2e.models.gpt_luna", "GPT Luna"])
def test_codex_picker_matches_model_only_in_numbered_rows(cursor, label):
    screen = f"Select Model and Effort\n  {cursor}1. {label} (current)\nEnter to confirm"
    assert cuj3.codex_model_in_picker(screen, cuj3.CODEX_DEFAULT, "GPT Luna")


@pytest.mark.parametrize(
    "screen",
    [
        "GPT Luna\nSelect Model and Effort\n  1. Other model",
        "Select Model and Effort\n  1. Other model\nCurrent model: GPT Luna",
        "  1. ug_e2e.models.gpt_luna_v2",
        "  1.\nGPT Luna",
        "",
    ],
)
def test_codex_picker_rejects_nonrows_and_different_model_ids(screen):
    assert not cuj3.codex_model_in_picker(screen, cuj3.CODEX_DEFAULT, "GPT Luna")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_accepts_exact_rows_and_ignores_banner_text(agent):
    models = cuj3.CLAUDE_MODELS if agent == "claude" else cuj3.CODEX_MODELS
    labels = {model: model.rsplit(".", 1)[-1].replace("_", " ").title() for model in models}
    rows = "\n".join(
        f"  {position}. {label}" for position, label in enumerate(labels.values(), start=1)
    )
    screen = f"Banner: Gemini Flash\nSelect model\n{rows}\nEnter to confirm"
    cuj3.assert_picker_inventory(screen, agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("excluded_label", ["Gemini Flash", "Friendly out-of-scope model"])
def test_picker_inventory_rejects_extra_friendly_label_rows(agent, excluded_label):
    models = cuj3.CLAUDE_MODELS if agent == "claude" else cuj3.CODEX_MODELS
    labels = dict.fromkeys(models, None)
    rows = "\n".join(f"  {position}. {model}" for position, model in enumerate(models, start=1))
    screen = f"Select model\n{rows}\n  {len(models) + 1}. {excluded_label}"
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(screen, agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_rejects_duplicate_and_missing_rows(agent):
    models = sorted(cuj3.CLAUDE_MODELS if agent == "claude" else cuj3.CODEX_MODELS)
    labels = dict.fromkeys(models, None)
    rows = "\n".join(f"  {position}. {model}" for position, model in enumerate(models, start=1))
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(f"{rows}\n  {len(models) + 1}. {models[0]}", agent, labels)
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(f"  1. {models[0]}", agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_rejects_ambiguous_labels_and_split_line_evidence(agent):
    models = sorted(cuj3.CLAUDE_MODELS if agent == "claude" else cuj3.CODEX_MODELS)
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(
            "  1. Shared label", agent, dict.fromkeys(models, "Shared label")
        )
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(f"  1.\n{models[0]}", agent, dict.fromkeys(models, None))


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("suffix", ["_v2", "-decoy", ".other"])
def test_picker_inventory_rejects_model_id_prefix_matches(agent, suffix):
    models = sorted(cuj3.CLAUDE_MODELS if agent == "claude" else cuj3.CODEX_MODELS)
    rows = "\n".join(
        f"  {position}. {model}{suffix if position == 1 else ''}"
        for position, model in enumerate(models, start=1)
    )
    with pytest.raises(AssertionError):
        cuj3.assert_picker_inventory(rows, agent, dict.fromkeys(models, None))


def test_model_policy_allows_server_metadata_and_later_stack_fields():
    config = json.loads(SCRIPT.with_name("cuj3-managed-config.json").read_text())
    config["update_time"] = "2026-10-02T00:00:00Z"
    config["name"] = "coding-agent-configs/cuj3"
    config["mcp_servers"] = {"unity_catalog_location": "ug_e2e.tools"}
    cuj3.assert_cuj3_config(config)


def test_model_policy_rejects_a_default_outside_the_managed_scope():
    config = json.loads(SCRIPT.with_name("cuj3-managed-config.json").read_text())
    config["enabled_agents"][1]["config"]["default_models"]["default_model"] = cuj3.CODEX_DECOY
    with pytest.raises(AssertionError):
        cuj3.assert_cuj3_config(config)


def test_codex_model_identity_uses_only_the_completed_answer_turn(monkeypatch):
    records = [
        {"type": "turn_context", "payload": {"turn_id": "other", "model": cuj3.CODEX_DECOY}},
        {"type": "turn_context", "payload": {"turn_id": "matching", "model": cuj3.CODEX_DEFAULT}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "matching",
                "last_agent_message": "withheld-file-value",
            },
        },
    ]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"transcript": records})
    assert cuj3.native_model_ids(None, "codex", "withheld-file-value") == {cuj3.CODEX_DEFAULT}


def test_codex_model_identity_rejects_prompt_only_evidence(monkeypatch):
    records = [
        {"type": "turn_context", "payload": {"turn_id": "matching", "model": cuj3.CODEX_DEFAULT}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "withheld-file-value"},
        },
    ]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"transcript": records})
    assert cuj3.native_model_ids(None, "codex", "withheld-file-value") == set()


def test_claude_model_identity_uses_assistant_answer_not_tool_output(monkeypatch):
    records = [
        {
            "type": "user",
            "message": {"model": cuj3.CLAUDE_DECOY, "content": [{"type": "text", "text": "value"}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": cuj3.CLAUDE_DEFAULT,
                "content": [{"type": "text", "text": "value"}],
            },
        },
    ]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"transcript": records})
    assert cuj3.native_model_ids(None, "claude", "value") == {cuj3.CLAUDE_DEFAULT}
