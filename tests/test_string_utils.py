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
        ("system.ai.claude-haiku-4-5", "Claude Haiku 4.5"),
        ("gpt-5.5", "GPT 5.5"),
        ("system.ai.gpt-5.5", "GPT 5.5"),
        ("system.ai.gpt-5-6-luna", "GPT 5.6 Luna"),
        ("system.ai.glm-5-3-flash", "GLM 5.3 Flash"),
        ("system.ai.kimi-k3", "Kimi K3"),
        ("system.ai.kimi-k3-1-pro", "Kimi K3.1 Pro"),
        ("system.ai.deepseek-v4-1-flash-1017", "DeepSeek V4.1 Flash 1017"),
        ("system.ai.qwen3-next-80b", "Qwen3 Next 80B"),
        ("o1-2024-12-17", "O1 2024.12.17"),
        ("a--1", "A  1"),
    ],
)
def test_model_picker_label(model, label):
    assert model_picker_label(model) == label
