"""AI_GATEWAY_REQUEST_TAGS reaches Claude per launch and is never written to a settings file."""

import json

import pytest

from ucode.agents import claude
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER

TAGS = '{"source":"isaac"}'
LINE = f"{AI_GATEWAY_REQUEST_TAGS_HEADER}: {TAGS}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("UG_HEADERS", raising=False)
    monkeypatch.delenv("AI_GATEWAY_REQUEST_TAGS", raising=False)


def _overlay(monkeypatch):
    monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
    monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
    env = claude.render_overlay("https://example.cloud.databricks.com", "m")[0]["env"]
    return env["ANTHROPIC_CUSTOM_HEADERS"]


def test_overlay_is_identical_with_and_without_tags(monkeypatch):
    plain = _overlay(monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    tagged = _overlay(monkeypatch)
    assert tagged == plain
    assert AI_GATEWAY_REQUEST_TAGS_HEADER not in tagged


def test_no_tags_does_nothing(monkeypatch):
    assert claude._deliver_request_tags(per_launch_settings=True) is None
    assert "UG_HEADERS" not in claude.os.environ


def test_header_mod_carries_tags_via_ug_headers(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    monkeypatch.setenv("UG_HEADERS", "X-Team: a")
    monkeypatch.setattr(claude, "header_mod_active", lambda: True)
    assert claude._deliver_request_tags(per_launch_settings=True) is None
    assert claude.os.environ["UG_HEADERS"] == f"X-Team: a\n{LINE}"


def test_header_mod_keeps_an_explicit_tags_header(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    explicit = f'{AI_GATEWAY_REQUEST_TAGS_HEADER}: {{"a":1}}'
    monkeypatch.setenv("UG_HEADERS", explicit)
    monkeypatch.setattr(claude, "header_mod_active", lambda: True)
    claude._deliver_request_tags(per_launch_settings=True)
    assert claude.os.environ["UG_HEADERS"] == explicit


def _no_mod(monkeypatch, tmp_path, managed_doc, saved_headers):
    managed = tmp_path / "managed-settings.json"
    if managed_doc is not None:
        managed.write_text(json.dumps(managed_doc), encoding="utf-8")
    saved = tmp_path / "ucode-settings.json"
    saved.write_text(
        json.dumps({"env": {"ANTHROPIC_CUSTOM_HEADERS": saved_headers}}), encoding="utf-8"
    )
    monkeypatch.setattr(claude, "header_mod_active", lambda: False)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed)
    monkeypatch.setattr(
        claude, "read_managed_file", lambda p: p.read_text() if p.exists() else None
    )
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", saved)
    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    return managed, saved


def test_without_managed_headers_tags_go_in_a_launch_override(monkeypatch, tmp_path):
    managed, saved = _no_mod(monkeypatch, tmp_path, None, "User-Agent: ug\nx-a: b")
    before = saved.read_text()
    override = claude._deliver_request_tags(per_launch_settings=True)
    assert override == {"env": {"ANTHROPIC_CUSTOM_HEADERS": f"User-Agent: ug\nx-a: b\n{LINE}"}}
    assert saved.read_text() == before
    assert not managed.exists()
    assert "UG_HEADERS" not in claude.os.environ


def test_override_replaces_a_stale_saved_tags_line(monkeypatch, tmp_path):
    _no_mod(monkeypatch, tmp_path, None, f"x-a: b\n{AI_GATEWAY_REQUEST_TAGS_HEADER}: {{}}")
    override = claude._deliver_request_tags(per_launch_settings=True)
    assert (override or {})["env"]["ANTHROPIC_CUSTOM_HEADERS"] == f"x-a: b\n{LINE}"


def test_managed_headers_without_mod_warn_and_write_nothing(monkeypatch, tmp_path, capsys):
    managed_doc = {"env": {"ANTHROPIC_CUSTOM_HEADERS": "x-a: b"}}
    managed, _ = _no_mod(monkeypatch, tmp_path, managed_doc, "x-a: b")
    before = managed.read_text()
    assert claude._deliver_request_tags(per_launch_settings=True) is None
    captured = capsys.readouterr()
    assert "ug configure" in captured.out + captured.err
    assert managed.read_text() == before
    assert "UG_HEADERS" not in claude.os.environ


def test_launch_without_override_support_warns(monkeypatch, tmp_path):
    _no_mod(monkeypatch, tmp_path, None, "x-a: b")
    warnings = []
    monkeypatch.setattr(claude, "print_warning", warnings.append)
    assert claude._deliver_request_tags(per_launch_settings=False) is None
    assert warnings and "AI_GATEWAY_REQUEST_TAGS" in warnings[0]


def test_write_tool_config_leaves_both_files_unchanged_by_tags(monkeypatch, tmp_path):
    from tests.test_claude_header_mod import WS, _managed_env

    managed_path, _, writes = _managed_env(tmp_path, monkeypatch)
    state = {"workspace": WS, "codex_models": []}
    claude.write_tool_config(state, "databricks-claude-sonnet-4")
    managed = managed_path.read_text()
    user = claude.CLAUDE_SETTINGS_PATH.read_text()
    count = len(writes)

    monkeypatch.setenv("AI_GATEWAY_REQUEST_TAGS", TAGS)
    claude.write_tool_config(state, "databricks-claude-sonnet-4")

    assert managed_path.read_text() == managed
    assert claude.CLAUDE_SETTINGS_PATH.read_text() == user
    assert len(writes) == count
    assert "Request-Tags" not in managed + user
