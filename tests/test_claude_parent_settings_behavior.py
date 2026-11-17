import json
from types import SimpleNamespace

import pytest

from tests.test_claude_header_mod import _managed_env
from ucode.agents import claude

WS = "https://example.cloud.databricks.com"
MODEL = "databricks-claude-sonnet-4"
STATE = {"workspace": WS, "codex_models": []}
KEY = "parentSettingsBehavior"


@pytest.fixture
def env(tmp_path, monkeypatch):
    managed_path, _root, writes = _managed_env(tmp_path, monkeypatch)
    claude.request_explicit_configure()
    yield SimpleNamespace(
        managed=managed_path, user=tmp_path / "ucode-settings.json", writes=writes
    )
    claude.request_explicit_configure(False)


def _managed(env):
    return json.loads(env.managed.read_text())


def test_configure_sets_merge_in_the_managed_file_only(env):
    claude.write_tool_config(dict(STATE), MODEL)
    assert _managed(env)[KEY] == "merge"
    assert KEY not in json.loads(env.user.read_text())
    assert env.writes.count(str(env.managed)) == 1


def test_configure_keeps_an_administrator_value(env):
    env.managed.write_text(json.dumps({KEY: "replace"}), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _managed(env)[KEY] == "replace"


def test_plain_launch_neither_adds_nor_removes_it(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert KEY not in _managed(env)

    claude.request_explicit_configure()
    claude.write_tool_config(dict(STATE), MODEL)
    count = len(env.writes)
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert len(env.writes) == count
    assert _managed(env)[KEY] == "merge"


def test_revert_removes_it_and_keeps_other_keys(env):
    env.managed.write_text(json.dumps({"env": {"KEEP": "1"}}), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _managed(env)[KEY] == "merge"

    claude.revert_managed_settings()
    assert _managed(env) == {"env": {"KEEP": "1"}}


def test_revert_keeps_an_administrator_value(env):
    env.managed.write_text(json.dumps({KEY: "replace"}), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    claude.revert_managed_settings()
    assert _managed(env) == {KEY: "replace"}


def test_revert_keeps_an_administrator_value_set_after_configure(env):
    claude.write_tool_config(dict(STATE), MODEL)
    doc = _managed(env)
    doc[KEY] = "replace"
    env.managed.write_text(json.dumps(doc), encoding="utf-8")
    claude.write_tool_config({**STATE, "claude_http_headers": {"x-team": "a"}}, MODEL)
    doc = _managed(env)
    doc["env"]["KEEP"] = "1"
    env.managed.write_text(json.dumps(doc), encoding="utf-8")

    claude.revert_managed_settings()
    assert _managed(env) == {KEY: "replace", "env": {"KEEP": "1"}}


def test_revert_removes_merge_after_a_launch_rewrite_and_an_external_edit(env):
    claude.write_tool_config(dict(STATE), MODEL)
    claude.request_explicit_configure(False)
    claude.write_tool_config({**STATE, "claude_http_headers": {"x-team": "a"}}, MODEL)
    doc = _managed(env)
    doc["env"]["KEEP"] = "1"
    env.managed.write_text(json.dumps(doc), encoding="utf-8")

    claude.revert_managed_settings()
    assert _managed(env) == {"env": {"KEEP": "1"}}
