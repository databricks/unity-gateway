"""Component checks for the reusable integration agent helper contracts."""

from types import SimpleNamespace

import pytest

from tests.integration.utils import evidence, model_discovery, provider_catalog
from tests.integration.utils.agents import claude, codex


def test_evidence_keeps_legacy_completed_task_names_as_adapter_aliases():
    assert evidence.claude_completed_task_models is claude.completed_task_models
    assert evidence.codex_completed_task_models is codex.completed_task_models


def test_agent_modules_expose_direct_model_discovery_helpers():
    assert claude.fetch_parent_catalog is provider_catalog.fetch_anthropic_parent_catalog
    assert claude.discovery_model_id is model_discovery.claude_discovery_model_id
    assert claude.model_service_id is model_discovery.claude_model_service_id
    assert claude.model_in_picker is model_discovery.claude_model_in_picker
    assert codex.fetch_parent_catalog is provider_catalog.fetch_codex_parent_catalog
    assert codex.model_in_picker is model_discovery.codex_model_in_picker


def test_agent_modules_preserve_exact_completion_attribution():
    claude_records = [
        {
            "type": "user",
            "message": {"model": "catalog.other_models.decoy", "content": []},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "catalog.models.claude_sonnet[1m]",
                "content": [{"type": "text", "text": "value"}],
            },
        },
    ]
    codex_records = [
        {
            "type": "turn_context",
            "payload": {"turn_id": "other", "model": "catalog.other_models.decoy"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "matching", "model": "catalog.models.gpt_luna"},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "matching",
                "last_agent_message": "value",
            },
        },
    ]

    assert claude.completed_task_models(claude_records, "value") == {"catalog.models.claude_sonnet"}
    assert codex.completed_task_models(codex_records, "value") == {"catalog.models.gpt_luna"}


def test_evidence_dispatches_assistant_answers_to_agent_modules():
    claude_records = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        }
    ]
    codex_records = [
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": "answer"},
        }
    ]

    assert evidence.assistant_answers("claude", claude_records) == ["answer"]
    assert evidence.assistant_answers("codex", codex_records) == ["answer"]


def test_unsupported_evidence_agents_fail_without_codex_fallback():
    session = SimpleNamespace(home=None, record=lambda *_: None)
    with pytest.raises(AssertionError, match="Unsupported evidence agent: opencode"):
        evidence.completed_task_models(session, "opencode", "value")
    with pytest.raises(AssertionError, match="Unsupported evidence agent: opencode"):
        evidence.assert_completed_task_model(session, "opencode", "value", "model")
