"""Tests for the `ug --header` / `UG_HEADERS` parser."""

from __future__ import annotations

import pytest

from ucode.ug_headers import ug_headers


def test_ug_headers_parse_one_per_line(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "x-databricks-traffic-id: testenv://liteswap/a\n\nX-B:  2 ")
    assert ug_headers() == {"x-databricks-traffic-id": "testenv://liteswap/a", "X-B": "2"}


@pytest.mark.parametrize("value", ["not a header", "x-databricks-traffic-id:  "])
def test_ug_headers_reject_malformed_line(monkeypatch, value):
    monkeypatch.setenv("UG_HEADERS", value)
    with pytest.raises(RuntimeError, match="UG_HEADERS"):
        ug_headers()
