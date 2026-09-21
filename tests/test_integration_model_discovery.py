"""Offline regression checks for integration discovery evidence validation."""

import pytest

from tests.integration.utils.model_discovery import claude_model_in_picker, claude_system_model_ids


@pytest.mark.parametrize("cursor", ["", "❯ ", "› ", "> "])
@pytest.mark.parametrize("label", ["system.ai.claude-sonnet-5", "Sonnet Custom"])
def test_claude_picker_matches_model_in_numbered_row(cursor, label):
    screen = f"Select model\n  {cursor}12. {label} (current)\nEnter to confirm"
    assert claude_model_in_picker(screen, "system.ai.claude-sonnet-5", "Sonnet Custom")


@pytest.mark.parametrize(
    "screen",
    [
        "Sonnet Custom · system.ai.claude-sonnet-5\nSelect model\n  1. Other model",
        "Select model\n  1. Other model\nCurrent model: Sonnet Custom",
        "Select model\n  1.\nSonnet Custom",
        "Select model\n  1. \nSonnet Custom",
        "Select model\n  1) Sonnet Custom",
        "Select model\n  Sonnet Custom",
        "",
    ],
)
def test_claude_picker_rejects_banner_footer_and_non_rows(screen):
    assert not claude_model_in_picker(screen, "system.ai.claude-sonnet-5", "Sonnet Custom")


@pytest.mark.parametrize("display_name", [None, "", " "])
def test_claude_picker_empty_name_cannot_match_an_unrelated_row(display_name):
    assert not claude_model_in_picker("  1. Other model", "", display_name)
    assert claude_model_in_picker("  1. system.ai.test", "system.ai.test", display_name)


def test_claude_picker_accepts_native_haiku_deduplication():
    assert claude_model_in_picker(
        "  ❯ 3. Haiku  Fast and efficient · Haiku 4.5",
        "claude-haiku-4-5-20251001",
        "Claude Haiku 4.5",
    )


@pytest.mark.parametrize(
    "screen",
    [
        "Haiku 4.5\nSelect model\n  1. Other model",
        "  3. Haiku  Fast and efficient · Haiku 3.5",
        "  3. Other model · Haiku 4.5",
    ],
)
def test_claude_picker_rejects_wrong_or_banner_only_haiku(screen):
    assert not claude_model_in_picker(screen, "claude-haiku-4-5-20251001", "Claude Haiku 4.5")


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
