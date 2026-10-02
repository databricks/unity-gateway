"""Component checks for CUJ3 discovery helpers and native model evidence."""

import io
import json
import runpy
import urllib.error
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.cuj3_fixture_data import INVALID_DESTINATIONS, model_readback
from tests.integration.utils import cuj3

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "fixtures/cuj/models/provision.py"


@pytest.fixture
def provisioner():
    return SimpleNamespace(**runpy.run_path(str(SCRIPT)))


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
    assert len(handlers) == len(expected)
    assert all(isinstance(handler, cuj3.NoRedirect) for handler in handlers)
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
    with pytest.raises(AssertionError, match=r"HTTP 404"):
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
    config = json.loads((ROOT / "fixtures/cuj-3/managed-config.json").read_text())
    config["update_time"] = "2026-10-02T00:00:00Z"
    config["name"] = "coding-agent-configs/cuj3"
    config["mcp_servers"] = {"unity_catalog_location": "ug_e2e.tools"}
    cuj3.assert_cuj3_config(config)


def test_model_policy_rejects_a_default_outside_the_managed_scope():
    config = json.loads((ROOT / "fixtures/cuj-3/managed-config.json").read_text())
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
    assert cuj3.completed_task_models(None, "codex", "withheld-file-value") == {cuj3.CODEX_DEFAULT}


def test_codex_model_identity_rejects_prompt_only_evidence(monkeypatch):
    records = [
        {"type": "turn_context", "payload": {"turn_id": "matching", "model": cuj3.CODEX_DEFAULT}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "withheld-file-value"},
        },
    ]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"transcript": records})
    assert cuj3.completed_task_models(None, "codex", "withheld-file-value") == set()


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
    assert cuj3.completed_task_models(None, "claude", "value") == {cuj3.CLAUDE_DEFAULT}


FETCHERS = [
    cuj3.fetch_model_service_inventory,
    cuj3.fetch_claude_parent_catalog,
    cuj3.fetch_codex_parent_catalog,
]


def fetch_inventory(fetcher, workspace="https://workspace.invalid", token="test-bearer"):
    if fetcher is cuj3.fetch_model_service_inventory:
        return fetcher(workspace, token)
    return fetcher(workspace, token, cuj3.MODEL_SCHEMA)


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize(
    "workspace",
    [
        None,
        7,
        "",
        "http://workspace.invalid",
        "https://user:secret@workspace.invalid",
        "https://workspace.invalid/path",
        "https://workspace.invalid?secret=value",
        "https://workspace.invalid#fragment",
        "https:///missing-host",
        "https://workspace.invalid:bad",
        "https://workspace.invalid:0",
        "https://workspace.invalid:70000",
        "https://workspace.invalid\n",
        "https://workspace.invalid\\@other.invalid",
    ],
)
def test_all_cuj3_gets_reject_invalid_workspace_origins(monkeypatch, fetcher, workspace):
    def unexpected_open(*args, **kwargs):
        pytest.fail("Invalid workspace must not reach the HTTP boundary")

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", unexpected_open)
    with pytest.raises(AssertionError, match="HTTPS workspace origin") as failure:
        fetch_inventory(fetcher, workspace)
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("token", [None, "", " ", "bearer\nsecret", "bearer\rsecret"])
def test_all_cuj3_gets_require_explicit_valid_bearers(monkeypatch, fetcher, token):
    def unexpected_open(*args, **kwargs):
        pytest.fail("Invalid bearer must not reach the HTTP boundary")

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", unexpected_open)
    with pytest.raises(AssertionError, match="explicit workspace bearer"):
        fetch_inventory(fetcher, token=token)


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("failure_kind", ["http", "url", "os", "json", "encoding"])
def test_all_cuj3_get_errors_are_sanitized(monkeypatch, fetcher, failure_kind):
    secret = "private-bearer-and-response-secret"

    class FailedOpener:
        def open(self, request, *, timeout):
            assert timeout == 30
            assert request.get_method() == "GET"
            assert request.get_header("Authorization") == f"Bearer {secret}"
            if failure_kind == "http":
                raise urllib.error.HTTPError(request.full_url, 403, secret, {}, None)
            if failure_kind == "url":
                raise urllib.error.URLError(secret)
            if failure_kind == "os":
                raise OSError(secret)
            return io.BytesIO(secret.encode() if failure_kind == "json" else b"\xff")

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: FailedOpener())
    with pytest.raises(AssertionError, match="Workspace JSON GET") as failure:
        fetch_inventory(fetcher, token=secret)
    assert secret not in str(failure.value)
    assert "workspace.invalid" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("fetcher", FETCHERS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["https://other.invalid/secret", "/redirected"])
def test_all_cuj3_gets_deny_redirects(monkeypatch, fetcher, status, destination):
    requests = []
    build_opener = cuj3.urllib.request.build_opener

    class BoundaryHTTPS(cuj3.urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            response = urllib.response.addinfourl(
                io.BytesIO(b"{}"), {"Location": destination}, request.full_url, status
            )
            response.msg = "private-redirect-reason"
            return response

    monkeypatch.setattr(
        cuj3.urllib.request,
        "build_opener",
        lambda handler: build_opener(handler, BoundaryHTTPS()),
    )
    with pytest.raises(AssertionError, match=f"HTTP {status}") as failure:
        fetch_inventory(fetcher)
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer test-bearer"
    assert destination not in str(failure.value)
    assert "private-redirect-reason" not in str(failure.value)


def test_claude_parent_catalog_retains_pages_labels_and_scoped_requests(monkeypatch):
    pages = [
        {
            "data": [{"id": cuj3.CLAUDE_DEFAULT, "display_name": "Sonnet"}],
            "has_more": True,
            "last_id": "cursor with / and ?",
        },
        {
            "data": [{"id": cuj3.claude_discovery_model_id(f"{cuj3.MODEL_SCHEMA}.kimi")}],
            "has_more": False,
        },
    ]
    requests = []

    class CatalogOpener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer test-bearer"
            assert request.get_header(cuj3.MODEL_HEADER.capitalize()) == cuj3.MODEL_SCHEMA
            assert request.get_header("Anthropic-version") == "2023-06-01"
            return io.BytesIO(json.dumps(pages[len(requests) - 1]).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: CatalogOpener())
    catalog = cuj3.fetch_claude_parent_catalog(
        "https://workspace.invalid/", "test-bearer", cuj3.MODEL_SCHEMA
    )
    assert catalog.payloads == tuple(pages)
    assert catalog.model_ids == (pages[0]["data"][0]["id"], pages[1]["data"][0]["id"])
    assert catalog.display_names == {catalog.model_ids[0]: "Sonnet", catalog.model_ids[1]: None}
    assert cuj3.urllib.parse.parse_qs(cuj3.urllib.parse.urlsplit(requests[1].full_url).query) == {
        "limit": ["1000"],
        "after_id": ["cursor with / and ?"],
    }


@pytest.mark.parametrize("failure_kind", ["duplicate", "cursor", "limit"])
def test_claude_parent_catalog_rejects_duplicate_or_unbounded_pagination(monkeypatch, failure_kind):
    requests = []

    class CatalogOpener:
        def open(self, request, *, timeout):
            requests.append(request)
            position = len(requests)
            payload = {
                "data": [
                    {"id": "duplicate" if failure_kind == "duplicate" else f"model-{position}"}
                ],
                "has_more": True,
                "last_id": "repeated" if failure_kind == "cursor" else f"cursor-{position}",
            }
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: CatalogOpener())
    with pytest.raises(AssertionError):
        cuj3.fetch_claude_parent_catalog(
            "https://workspace.invalid", "test-bearer", cuj3.MODEL_SCHEMA
        )
    assert len(requests) == (20 if failure_kind == "limit" else 2)


def test_codex_parent_catalog_retains_unfiltered_payload_and_validates_duplicates(monkeypatch):
    payload = {
        "models": [
            {"slug": cuj3.CODEX_DEFAULT, "display_name": "Luna", "visibility": "list"},
            {"slug": cuj3.CODEX_DECOY, "visibility": "hidden"},
        ]
    }

    class CatalogOpener:
        def open(self, request, *, timeout):
            assert timeout == 30
            assert request.get_header(cuj3.MODEL_HEADER.capitalize()) == cuj3.MODEL_SCHEMA
            return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(cuj3.urllib.request, "build_opener", lambda *_: CatalogOpener())
    catalog = cuj3.fetch_codex_parent_catalog(
        "https://workspace.invalid", "test-bearer", cuj3.MODEL_SCHEMA
    )
    assert catalog.payloads == (payload,)
    assert catalog.model_ids == (cuj3.CODEX_DEFAULT,)
    assert catalog.display_names == {cuj3.CODEX_DEFAULT: "Luna"}
    payload["models"].append(payload["models"][0])
    with pytest.raises(AssertionError, match="repeated"):
        cuj3.fetch_codex_parent_catalog(
            "https://workspace.invalid", "test-bearer", cuj3.MODEL_SCHEMA
        )


def completed_records(agent, model, answer="value", turn_id="matching"):
    if agent == "claude":
        return [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": answer}],
                },
            }
        ]
    return [
        {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model}},
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": answer},
        },
    ]


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("parent_answer", [None, "value", "unrelated-answer"])
def test_completed_task_models_excludes_child_only_and_conflicting_child_evidence(
    monkeypatch, agent, parent_answer
):
    child = completed_records(agent, "child-model")
    if agent == "codex":
        child.insert(0, {"type": "session_meta", "payload": {"source": {"subagent": "spawn"}}})
    sessions = {"project/subagents/child.jsonl": child}
    if parent_answer is not None:
        sessions["project/parent.jsonl"] = completed_records(agent, "parent-model", parent_answer)
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: sessions)
    expected = {"parent-model"} if parent_answer == "value" else set()
    assert cuj3.completed_task_models(None, agent, "value") == expected


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("model", [None, "", " ", 5, {}, [], "model with whitespace"])
def test_completed_task_models_fails_closed_on_malformed_model(monkeypatch, agent, model):
    records = completed_records(agent, model)
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"parent": records})
    with pytest.raises(AssertionError, match="model"):
        cuj3.completed_task_models(None, agent, "value")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_completed_task_models_fails_closed_on_missing_model(monkeypatch, agent):
    records = completed_records(agent, "model")
    del records[0]["message" if agent == "claude" else "payload"]["model"]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"parent": records})
    with pytest.raises(AssertionError, match="model"):
        cuj3.completed_task_models(None, agent, "value")


@pytest.mark.parametrize("turn_id", [None, "", " ", 5, {}, [], "turn with whitespace"])
@pytest.mark.parametrize("record_index", [0, 1])
def test_codex_completed_task_models_fails_closed_on_malformed_turn_ids(turn_id, record_index):
    records = completed_records("codex", "model")
    records[record_index]["payload"]["turn_id"] = turn_id
    with pytest.raises(AssertionError, match="turn ID"):
        cuj3.codex_completed_task_models(records, "value")


@pytest.mark.parametrize("record_index", [0, 1])
def test_codex_completed_task_models_fails_closed_on_missing_turn_ids(record_index):
    records = completed_records("codex", "model")
    del records[record_index]["payload"]["turn_id"]
    with pytest.raises(AssertionError, match="turn ID"):
        cuj3.codex_completed_task_models(records, "value")


def test_codex_completed_task_models_requires_context_in_same_session(monkeypatch):
    context, completion = completed_records("codex", "model")
    monkeypatch.setattr(
        cuj3,
        "agent_sessions",
        lambda *_: {"context-session": [context], "answer-session": [completion]},
    )
    with pytest.raises(AssertionError, match="Missing Codex model context"):
        cuj3.completed_task_models(None, "codex", "value")


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("models", [[], ["unexpected"], ["expected", "conflicting"]])
def test_completed_task_model_assertion_requires_exact_singleton(monkeypatch, agent, models):
    records = [record for model in models for record in completed_records(agent, model)]
    monkeypatch.setattr(cuj3, "agent_sessions", lambda *_: {"parent": records})
    evidence = {}
    session = SimpleNamespace(record=lambda name, payload: evidence.update({name: payload}))
    with pytest.raises(AssertionError):
        cuj3.assert_completed_task_model(session, agent, "value", "expected")
    assert next(iter(evidence.values()))["observed"] == sorted(models)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_completed_task_model_records_evidence_limits(monkeypatch, agent):
    monkeypatch.setattr(
        cuj3, "agent_sessions", lambda *_: {"parent": completed_records(agent, "expected")}
    )
    evidence = {}
    session = SimpleNamespace(record=lambda name, payload: evidence.update({name: payload}))
    cuj3.assert_completed_task_model(session, agent, "value", "expected")
    assert evidence == {
        f"completed-task-model-{agent}-value.json": {
            "expected": "expected",
            "observed": ["expected"],
            "evidence_kind": (
                "response-reported model" if agent == "claude" else "client-selected model"
            ),
            "gateway_destination_proven": False,
        }
    }


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("answer_value", [None, "", " \t", 1, [], {}])
def test_completed_task_models_rejects_empty_or_malformed_expected_answers(
    monkeypatch, agent, answer_value
):
    def unexpected_read(*args):
        pytest.fail("Invalid answer must not read session evidence")

    monkeypatch.setattr(cuj3, "agent_sessions", unexpected_read)
    with pytest.raises(AssertionError, match="nonempty answer value"):
        cuj3.completed_task_models(None, agent, answer_value)
    adapter = (
        cuj3.claude_completed_task_models if agent == "claude" else cuj3.codex_completed_task_models
    )
    with pytest.raises(AssertionError, match="nonempty answer value"):
        adapter([], answer_value)


@pytest.mark.parametrize(
    "message",
    [None, [], {}, {"role": "assistant"}, {"role": "assistant", "content": "value"}],
)
def test_claude_completed_task_models_rejects_malformed_message_metadata(message):
    with pytest.raises(AssertionError):
        cuj3.claude_completed_task_models([{"type": "assistant", "message": message}], "value")


@pytest.mark.parametrize("content", [[None], [{"type": "text"}], [{"type": "text", "text": 4}]])
def test_claude_completed_task_models_rejects_malformed_answer_blocks(content):
    records = completed_records("claude", "model")
    records[0]["message"]["content"] = content
    with pytest.raises(AssertionError):
        cuj3.claude_completed_task_models(records, "value")


@pytest.mark.parametrize("payload", [None, [], "value"])
@pytest.mark.parametrize("record_type", ["event_msg", "turn_context"])
def test_codex_completed_task_models_rejects_malformed_payloads(payload, record_type):
    with pytest.raises(AssertionError):
        cuj3.codex_completed_task_models([{"type": record_type, "payload": payload}], "value")


@pytest.mark.parametrize("answer", [None, [], {}, 7])
def test_codex_completed_task_models_rejects_malformed_completed_answers(answer):
    records = completed_records("codex", "model")
    records[1]["payload"]["last_agent_message"] = answer
    with pytest.raises(AssertionError, match="completed answer"):
        cuj3.codex_completed_task_models(records, "value")


def test_claude_completed_task_models_normalizes_only_context_window_suffix():
    assert cuj3.claude_completed_task_models(
        completed_records("claude", f"{cuj3.CLAUDE_DEFAULT}[1m]"), "value"
    ) == {cuj3.CLAUDE_DEFAULT}
