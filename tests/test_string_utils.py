"""Tests for string helpers."""

from datetime import UTC, datetime

import pytest

from ucode.string_utils import is_valid_catalog_schema, parse_update_time


class TestParseUpdateTime:
    def test_whole_second_and_subsecond_forms_compare_chronologically(self):
        earlier = parse_update_time("2026-06-26T05:58:25Z")
        later = parse_update_time("2026-06-26T05:58:25.400Z")
        assert earlier is not None and later is not None
        assert later > earlier  # the case a raw string compare gets wrong

    def test_offsetless_value_is_pinned_to_utc(self):
        assert parse_update_time("2026-06-26T05:58:25") == datetime(
            2026, 6, 26, 5, 58, 25, tzinfo=UTC
        )

    @pytest.mark.parametrize("value", [None, "", "not-a-time"])
    def test_missing_or_unparseable_is_none(self, value):
        assert parse_update_time(value) is None


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
