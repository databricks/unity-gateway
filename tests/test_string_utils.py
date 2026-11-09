"""Tests for string validation helpers."""

import pytest

from ucode.string_utils import (
    is_valid_catalog_schema,
    is_valid_catalog_schema_name,
    model_picker_label,
)


@pytest.mark.parametrize("value", ["system.ai", "main.default", "my-catalog.my_schema"])
def test_catalog_schema(value):
    assert is_valid_catalog_schema(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "main",
        "main.default.extra",
        ".default",
        "main.",
        "main/development.models",
        "main dev.models",
        "main.\tmodels",
        "main.\x7fmodels",
    ],
)
def test_invalid_catalog_schema(value):
    assert not is_valid_catalog_schema(value)


@pytest.mark.parametrize("value", ["main.default.anthropic", "my-catalog.my_schema.my-mps"])
def test_catalog_schema_name(value):
    assert is_valid_catalog_schema_name(value)


@pytest.mark.parametrize(
    "value", ["", "main.default", "main.default.mps.extra", "main..mps", "main.default.my mps"]
)
def test_invalid_catalog_schema_name(value):
    assert not is_valid_catalog_schema_name(value)


@pytest.mark.parametrize(
    ("model", "label"),
    [
        # system.ai ids and bare ids keep their established labels exactly.
        ("system.ai.claude-opus-5-5", "Claude Opus 5.5"),
        ("system.ai.claude-haiku-4-5", "Claude Haiku 4.5"),
        ("system.ai.gpt-5-6-sol", "GPT 5.6 Sol"),
        ("system.ai.glm-5-3", "GLM 5.3"),
        ("system.ai.kimi-k3", "Kimi K3"),
        ("databricks-custom-model", "Databricks Custom Model"),
        ("claude-opus-5-5", "Claude Opus 5.5"),
        # A dotted version inside the model name is not a namespace separator.
        ("gpt-5.5", "GPT 5.5"),
        ("glm-5.3", "GLM 5.3"),
        ("system.ai.gpt-5.5", "GPT 5.5"),
        ("system.ai.llama-3.3-70b-instruct", "Llama 3.3 70B Instruct"),
        # A non-system.ai namespace is kept so same-model entries from different
        # sources stay distinct.
        ("main.anthropic.byok.claude-opus-5-5", "Claude Opus 5.5 (main.anthropic.byok)"),
        ("main.anthropic.byok-2.claude-sonnet-5-5", "Claude Sonnet 5.5 (main.anthropic.byok-2)"),
    ],
)
def test_model_picker_label(model, label):
    assert model_picker_label(model) == label
