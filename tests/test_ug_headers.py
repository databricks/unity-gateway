"""Tests for the `ug --header` / `UG_HEADERS` parser."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from ucode.cli import app
from ucode.ug_headers import ug_headers


def test_ug_headers_parse_one_per_line(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "x-databricks-traffic-id: testenv://liteswap/a\n\nX-B:  2 ")
    assert ug_headers() == {"x-databricks-traffic-id": "testenv://liteswap/a", "X-B": "2"}


@pytest.mark.parametrize("value", ["not a header", "x-databricks-traffic-id:  "])
def test_ug_headers_reject_malformed_line(monkeypatch, value):
    monkeypatch.setenv("UG_HEADERS", value)
    with pytest.raises(RuntimeError, match="UG_HEADERS"):
        ug_headers()


def test_header_flag_appends_to_ug_headers(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "X-Env: 1")
    result = CliRunner().invoke(app, ["--header", "X-Flag: 2", "--header", "bad", "status"])
    assert result.exit_code == 1
    assert "expected `Name: value`" in result.output

    # The rejected launch exits ug in real use; reset what it left in this shared process.
    monkeypatch.setenv("UG_HEADERS", "X-Env: 1")
    # `--help` on the subcommand still runs ug's own callback, which sets the variable, first.
    result = CliRunner().invoke(app, ["--header", "X-Flag: 2", "status", "--help"])
    assert result.exit_code == 0, result.output
    assert os.environ["UG_HEADERS"] == "X-Env: 1\nX-Flag: 2"
