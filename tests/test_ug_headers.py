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


@pytest.mark.parametrize(
    "line",
    [
        "not a header",
        "x-databricks-traffic-id:  ",
        "bad name: 1",
        "X-Control: a\x01b",
        "X-Unicode: 日本",
    ],
)
def test_ug_headers_skip_invalid_line(monkeypatch, line):
    monkeypatch.setenv("UG_HEADERS", f"{line}\nX-Ok: 1")
    assert ug_headers() == {"X-Ok": "1"}


def test_ug_headers_keep_tab_and_latin1_values(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "X-Tab: a\tb\nX-Latin: caf\xe9")
    assert ug_headers() == {"X-Tab": "a\tb", "X-Latin": "caf\xe9"}


def test_later_line_replaces_same_name_in_any_case(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "X-A: 1\nx-a: 2")
    assert ug_headers() == {"x-a": "2"}


def test_header_flag_appends_to_ug_headers(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "X-Env: 1")
    result = CliRunner().invoke(
        app, ["--header", "X-Flag: 2", "--header", "Authorization Bearer secret-tok", "status"]
    )
    assert result.exit_code == 1
    assert "--header #2: expected `Name: value`" in result.output
    # The value may be a token, so the error must not echo it.
    assert "secret-tok" not in result.output

    # The rejected launch exits ug in real use; reset what it left in this shared process.
    monkeypatch.setenv("UG_HEADERS", "X-Env: 1")
    # `--help` on the subcommand still runs ug's own callback, which sets the variable, first.
    result = CliRunner().invoke(app, ["--header", "X-Flag: 2", "status", "--help"])
    assert result.exit_code == 0, result.output
    assert os.environ["UG_HEADERS"] == "X-Env: 1\nX-Flag: 2"


def test_bad_inherited_line_never_blocks_a_command(monkeypatch):
    monkeypatch.setenv("UG_HEADERS", "Authorization Bearer secret-tok\nX-Ok: 1")
    result = CliRunner().invoke(app, ["revert", "--help"])
    assert result.exit_code == 0, result.output
    assert "secret-tok" not in result.output


def test_openui_attribution_is_launch_local(monkeypatch):
    monkeypatch.setenv("OPENUI_SESSION_ID", "session-123")
    monkeypatch.setenv("UG_HEADERS", "X-Team: eng")

    assert ug_headers() == {
        "X-Team": "eng",
        "Databricks-Custom-Client-Surfaces": "openui",
    }
    assert os.environ["UG_HEADERS"] == "X-Team: eng"

    monkeypatch.delenv("OPENUI_SESSION_ID")
    assert ug_headers() == {"X-Team": "eng"}


@pytest.mark.parametrize("session_id", ["", " \t"])
def test_empty_openui_marker_does_not_attribute_a_terminal_launch(monkeypatch, session_id):
    monkeypatch.setenv("OPENUI_SESSION_ID", session_id)
    assert ug_headers() == {}


def test_openui_merges_surfaces_and_keeps_admin_headers(monkeypatch):
    monkeypatch.setenv("OPENUI_SESSION_ID", "session-123")
    monkeypatch.setenv(
        "UG_HEADERS",
        "x-team: caller\ndatabricks-custom-client-surfaces: UG, OpenUI\nX-New: 1",
    )
    existing = {
        "User-Agent": "ucode/1 claude/2",
        "X-Team": "admin",
        "Databricks-Custom-Client-Surfaces": "omnigent",
    }

    merged = ug_headers(existing)

    assert merged == {
        "User-Agent": "ucode/1 claude/2",
        "X-Team": "admin",
        "Databricks-Custom-Client-Surfaces": "omnigent, UG, OpenUI",
        "X-New": "1",
    }
    assert ug_headers(merged) == merged
    assert existing["Databricks-Custom-Client-Surfaces"] == "omnigent"


def test_client_surfaces_merge_repeated_header_names_and_values(monkeypatch):
    monkeypatch.setenv("OPENUI_SESSION_ID", "session-123")
    monkeypatch.setenv(
        "UG_HEADERS",
        "databricks-custom-client-surfaces: ug, OpenUI\n"
        "Databricks-Custom-Client-Surfaces: UG, other",
    )

    assert ug_headers() == {
        "databricks-custom-client-surfaces": "ug, OpenUI, other",
    }
