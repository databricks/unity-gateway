"""Tests for string validation helpers."""

import pytest

from ucode.string_utils import is_valid_catalog_schema, is_valid_catalog_schema_name


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
