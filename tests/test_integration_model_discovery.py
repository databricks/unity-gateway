"""Offline regression checks for integration discovery evidence validation."""

import pytest

from tests.integration.utils.model_discovery import (
    assert_picker_inventory,
    claude_discovery_model_id,
    claude_model_in_picker,
    claude_system_model_ids,
    codex_model_in_picker,
)

PICKER_MODELS = {
    "claude": frozenset(
        {"catalog.models.claude_sonnet", "catalog.models.claude_haiku", "catalog.models.kimi"}
    ),
    "codex": frozenset({"catalog.models.gpt_luna", "catalog.models.kimi"}),
}


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


def test_claude_picker_accepts_catalog_haiku_id_and_display_name():
    screen = "Select model\n  ❯ 2. claude-haiku-4-5-20251001 ✔  claude-haiku-4-5-20251001"
    assert claude_model_in_picker(
        screen,
        "claude-haiku-4-5-20251001",
        "claude-haiku-4-5-20251001",
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


def test_claude_discovery_requires_exact_gateway_wire_ids():
    assert {claude_discovery_model_id(model) for model in PICKER_MODELS["claude"]} == {
        "catalog.models.claude_haiku",
        "catalog.models.claude_sonnet",
        "anthropic-aigw-69e2a9a0-catalog.models.kimi",
    }
    assert (
        claude_discovery_model_id("catalog.other_models.claude_decoy")
        == "catalog.other_models.claude_decoy"
    )


@pytest.mark.parametrize("cursor", ["", "❯ ", "› ", "> "])
@pytest.mark.parametrize("label", ["catalog.models.gpt_luna", "GPT Luna"])
def test_codex_picker_matches_model_only_in_numbered_rows(cursor, label):
    screen = f"Select Model and Effort\n  {cursor}1. {label} (current)\nEnter to confirm"
    assert codex_model_in_picker(screen, "catalog.models.gpt_luna", "GPT Luna")


@pytest.mark.parametrize(
    "screen",
    [
        "GPT Luna\nSelect Model and Effort\n  1. Other model",
        "Select Model and Effort\n  1. Other model\nCurrent model: GPT Luna",
        "  1. catalog.models.gpt_luna_v2",
        "  1.\nGPT Luna",
        "",
    ],
)
def test_codex_picker_rejects_nonrows_and_different_model_ids(screen):
    assert not codex_model_in_picker(screen, "catalog.models.gpt_luna", "GPT Luna")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_accepts_exact_rows_and_ignores_banner_text(agent):
    models = PICKER_MODELS["claude"] if agent == "claude" else PICKER_MODELS["codex"]
    labels = {model: model.rsplit(".", 1)[-1].replace("_", " ").title() for model in models}
    rows = "\n".join(
        f"  {position}. {label}" for position, label in enumerate(labels.values(), start=1)
    )
    screen = f"Banner: Gemini Flash\nSelect model\n{rows}\nEnter to confirm"
    assert_picker_inventory(screen, agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("excluded_label", ["Gemini Flash", "Friendly out-of-scope model"])
def test_picker_inventory_rejects_extra_friendly_label_rows(agent, excluded_label):
    models = PICKER_MODELS["claude"] if agent == "claude" else PICKER_MODELS["codex"]
    labels = dict.fromkeys(models, None)
    rows = "\n".join(f"  {position}. {model}" for position, model in enumerate(models, start=1))
    screen = f"Select model\n{rows}\n  {len(models) + 1}. {excluded_label}"
    with pytest.raises(AssertionError):
        assert_picker_inventory(screen, agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_rejects_duplicate_and_missing_rows(agent):
    models = sorted(PICKER_MODELS["claude"] if agent == "claude" else PICKER_MODELS["codex"])
    labels = dict.fromkeys(models, None)
    rows = "\n".join(f"  {position}. {model}" for position, model in enumerate(models, start=1))
    with pytest.raises(AssertionError):
        assert_picker_inventory(f"{rows}\n  {len(models) + 1}. {models[0]}", agent, labels)
    with pytest.raises(AssertionError):
        assert_picker_inventory(f"  1. {models[0]}", agent, labels)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_picker_inventory_rejects_ambiguous_labels_and_split_line_evidence(agent):
    models = sorted(PICKER_MODELS["claude"] if agent == "claude" else PICKER_MODELS["codex"])
    with pytest.raises(AssertionError):
        assert_picker_inventory("  1. Shared label", agent, dict.fromkeys(models, "Shared label"))
    with pytest.raises(AssertionError):
        assert_picker_inventory(f"  1.\n{models[0]}", agent, dict.fromkeys(models, None))


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("suffix", ["_v2", "-decoy", ".other"])
def test_picker_inventory_rejects_model_id_prefix_matches(agent, suffix):
    models = sorted(PICKER_MODELS["claude"] if agent == "claude" else PICKER_MODELS["codex"])
    rows = "\n".join(
        f"  {position}. {model}{suffix if position == 1 else ''}"
        for position, model in enumerate(models, start=1)
    )
    with pytest.raises(AssertionError):
        assert_picker_inventory(rows, agent, dict.fromkeys(models, None))
