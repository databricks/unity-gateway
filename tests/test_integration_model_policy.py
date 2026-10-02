"""Component checks for catalog discovery model-policy expectations."""

import pytest

from tests.integration import catalog_discovery_expectations as expectations


@pytest.fixture
def managed_policy():
    return {
        "spec_version": 1,
        "default_agent": "CODING_AGENT_CLAUDE_CODE",
        "enabled_agents": [
            {
                "agent": "CODING_AGENT_CLAUDE_CODE",
                "config": {
                    "models": {"unity_catalog_location": "ug_e2e.models"},
                    "default_models": {
                        "default_model": "ug_e2e.models.claude_sonnet",
                        "default_sonnet_model": "ug_e2e.models.claude_sonnet",
                    },
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            },
            {
                "agent": "CODING_AGENT_CODEX",
                "config": {
                    "models": {"unity_catalog_location": "ug_e2e.models"},
                    "default_models": {"default_model": "ug_e2e.models.gpt_luna"},
                    "smart_routing": {"enabled": False},
                    "tracing": {"enabled": False},
                },
            },
        ],
    }


def test_model_discovery_matrix_filters_families_without_changing_schema_scope():
    expected_models = {
        "ug_e2e.models.gpt_luna",
        "ug_e2e.models.claude_haiku",
        "ug_e2e.models.claude_sonnet",
        "ug_e2e.models.kimi",
        "ug_e2e.models.gemini_flash",
    }
    assert expectations.MODEL_SERVICES == expected_models
    assert expectations.CLAUDE_MODELS == {
        "ug_e2e.models.claude_haiku",
        "ug_e2e.models.claude_sonnet",
        "ug_e2e.models.kimi",
    }
    assert expectations.CODEX_MODELS == {"ug_e2e.models.gpt_luna", "ug_e2e.models.kimi"}
    assert expectations.GEMINI_MODEL in expectations.MODEL_SERVICES
    assert expectations.GEMINI_MODEL not in expectations.CLAUDE_MODELS | expectations.CODEX_MODELS
    assert expectations.CLAUDE_DEFAULT in expectations.CLAUDE_MODELS
    assert expectations.CODEX_DEFAULT in expectations.CODEX_MODELS
    assert expectations.CLAUDE_DECOY not in expectations.MODEL_SERVICES
    assert expectations.CODEX_DECOY not in expectations.MODEL_SERVICES


def test_model_policy_allows_server_metadata_and_later_stack_fields(managed_policy):
    managed_policy["update_time"] = "2026-10-02T00:00:00Z"
    managed_policy["name"] = "coding-agent-configs/cuj3"
    managed_policy["mcp_servers"] = {"unity_catalog_location": "ug_e2e.tools"}
    expectations.assert_model_policy(managed_policy)


def test_model_policy_rejects_a_default_outside_the_managed_scope(managed_policy):
    managed_policy["enabled_agents"][1]["config"]["default_models"]["default_model"] = (
        expectations.CODEX_DECOY
    )
    with pytest.raises(AssertionError):
        expectations.assert_model_policy(managed_policy)
