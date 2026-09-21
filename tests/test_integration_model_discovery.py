"""Offline regression checks for integration discovery evidence validation."""

import pytest

from tests.integration.utils.model_discovery import claude_system_model_ids


def test_claude_system_model_ids_accepts_native_ids_and_gateway_aliases():
    models = [
        {"id": "system.ai.claude-sonnet-5"},
        {"id": "anthropic-aigw-73ea02b2-system.ai.glm-5-2"},
        {"id": "anthropic-aigw-BA377E31-system.ai.kimi-k3"},
    ]
    assert claude_system_model_ids(models) == [
        "system.ai.claude-sonnet-5",
        "system.ai.glm-5-2",
        "system.ai.kimi-k3",
    ]
    # Evidence remains the raw agent response; normalization only affects comparisons.
    assert models[1]["id"] == "anthropic-aigw-73ea02b2-system.ai.glm-5-2"


@pytest.mark.parametrize(
    "model_id",
    [
        None,
        123,
        "",
        "system.ai.",
        "main.ucode.custom_model",
        "anthropic-aigw-73ea02b-system.ai.glm-5-2",
        "anthropic-aigw-73ea02b22-system.ai.glm-5-2",
        "anthropic-aigw-zzzzzzzz-system.ai.glm-5-2",
        "anthropic-aigw-73ea02b2-main.ucode.custom_model",
        "anthropic-aigw-73ea02b2-system.ai.",
        "unexpected-system.ai.glm-5-2",
    ],
)
def test_claude_system_model_ids_rejects_invalid_or_out_of_scope_ids(model_id):
    with pytest.raises(AssertionError):
        claude_system_model_ids([{"id": "system.ai.claude-sonnet-5"}, {"id": model_id}])


@pytest.mark.parametrize(
    "models",
    [
        [],
        [{}],
        [{"id": "system.ai.claude-sonnet-5"}] * 2,
        [{"id": "anthropic-aigw-73ea02b2-system.ai.glm-5-2"}] * 2,
    ],
)
def test_claude_system_model_ids_rejects_empty_missing_and_duplicate_ids(models):
    with pytest.raises(AssertionError):
        claude_system_model_ids(models)
