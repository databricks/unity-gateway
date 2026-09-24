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


@pytest.mark.parametrize(
    ("model_id", "row", "wrong_version"),
    [
        ("claude-haiku-4-5", "Haiku  Haiku 4.5 · Fastest", "Haiku  Haiku 3.5"),
        ("claude-opus-5", "Opus (1M context)  Opus 5 with 1M context", "Opus  Opus 4.8"),
        ("claude-sonnet-5", "Sonnet ✔  Sonnet 5 · Efficient", "Sonnet  Sonnet 4.6"),
    ],
)
def test_claude_picker_accepts_only_matching_native_family_versions(model_id, row, wrong_version):
    assert claude_model_in_picker(f"  ❯ 3. {row}", model_id, model_id)
    assert claude_model_in_picker(f"  3. Custom model ({model_id})", model_id, model_id)
    assert not claude_model_in_picker(f"  3. {wrong_version}", model_id, model_id)
    assert not claude_model_in_picker(f"{row}\n  3. Other model", model_id, model_id)
    assert not claude_model_in_picker(f"  3. {row}", f"system.ai.{model_id}", model_id)


def test_claude_picker_does_not_confuse_native_minor_versions():
    assert not claude_model_in_picker("  3. Sonnet  Sonnet 5.1", "claude-sonnet-5", None)


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
