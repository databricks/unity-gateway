import json
from types import SimpleNamespace

import pytest

from tests.test_claude_header_mod import _managed_env
from ucode.agents import claude

WS = "https://example.cloud.databricks.com"
BLANKS = {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}
MODEL = "databricks-claude-sonnet-4"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The header-mod tests' real reconcile flow, run as `ug configure`; tests call `request_explicit_configure(False)` for a launch."""
    managed_path, _root, writes = _managed_env(tmp_path, monkeypatch)
    claude.request_explicit_configure()
    yield SimpleNamespace(
        managed=managed_path, user=tmp_path / "ucode-settings.json", writes=writes, mp=monkeypatch
    )
    claude.request_explicit_configure(False)


def _env(path):
    return json.loads(path.read_text())["env"]


def _blanks(path):
    return {k: v for k, v in _env(path).items() if k in BLANKS}


def _drop_blanks_like_a_second_writer(path):
    doc = json.loads(path.read_text())
    for key in BLANKS:
        doc["env"].pop(key)
    path.write_text(json.dumps(doc), encoding="utf-8")


STATE = {"workspace": WS, "codex_models": []}


def test_overlay_blanks_both_for_helper_and_relayed_launches():
    for relayed in (False, True):
        overlay, keys = claude.render_overlay(
            WS,
            MODEL,
            {},
            relayed=relayed,
            relayed_base_url="http://127.0.0.1:9" if relayed else None,
        )
        assert {k: overlay["env"][k] for k in BLANKS} == BLANKS
        assert all(["env", k] in keys for k in BLANKS)


def test_configure_writes_blanks_to_both_files(env):
    claude.write_tool_config(dict(STATE), MODEL)
    assert _blanks(env.user) == BLANKS
    assert _blanks(env.managed) == BLANKS
    assert env.writes.count(str(env.managed)) == 1  # same privileged write as the rest of the file


def test_plain_launch_after_second_writer_dropped_them_makes_no_write(env):
    claude.write_tool_config(dict(STATE), MODEL)
    _drop_blanks_like_a_second_writer(env.managed)
    before = env.managed.read_bytes()
    count = len(env.writes)
    claude.request_explicit_configure(False)

    claude.write_tool_config(dict(STATE), MODEL)  # no conflict raised, no sudo
    claude.write_tool_config(dict(STATE), MODEL)

    assert len(env.writes) == count
    assert env.managed.read_bytes() == before
    assert _blanks(env.managed) == {}
    assert _blanks(env.user) == BLANKS


def test_plain_launch_does_not_add_blanks_to_a_fresh_managed_file(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert _blanks(env.managed) == {}
    assert _blanks(env.user) == BLANKS


def test_plain_launch_leaves_present_blanks_and_values_alone(env):
    claude.write_tool_config(dict(STATE), MODEL)
    count = len(env.writes)
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert len(env.writes) == count
    assert _blanks(env.managed) == BLANKS

    # A value another party set is neither a conflict nor rewritten by a plain launch.
    doc = json.loads(env.managed.read_text())
    doc["env"]["ANTHROPIC_API_KEY"] = "admin-set"
    env.managed.write_text(json.dumps(doc), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    assert len(env.writes) == count
    assert _env(env.managed)["ANTHROPIC_API_KEY"] == "admin-set"
    assert _blanks(env.user) == BLANKS


def test_conflicts_ignore_the_blanks():
    existing = {"env": {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "a"}}
    desired = {"env": {"ANTHROPIC_BASE_URL": "b"}}
    paths = [*claude.CLAUDE_BLANKED_CREDENTIAL_PATHS, ["env", "ANTHROPIC_BASE_URL"]]
    assert claude._managed_settings_conflicts(existing, desired, paths) == [
        "env.ANTHROPIC_BASE_URL"
    ]


def test_relayed_launch_blanks_user_file_and_leaves_managed_file_alone(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL, relayed=True)
    doc = json.loads(env.user.read_text())
    assert "apiKeyHelper" not in doc
    assert _blanks(env.user) == BLANKS
    assert not env.managed.exists()
    assert env.writes == []


def test_skip_mode_writes_only_the_user_file(env):
    env.mp.setenv("UG_SKIP_OS_SETTINGS", "1")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _blanks(env.user) == BLANKS
    assert not env.managed.exists()
    assert env.writes == []


def test_revert_removes_blanks_from_the_managed_file(env):
    env.managed.write_text(json.dumps({"env": {"KEEP": "1"}}), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _blanks(env.managed) == BLANKS

    claude.revert_managed_settings()
    assert json.loads(env.managed.read_text()) == {"env": {"KEEP": "1"}}


def test_revert_removes_blanks_added_by_configure_after_a_launch_rewrite(env):
    claude.write_tool_config(dict(STATE), MODEL)
    claude.request_explicit_configure(False)
    claude.write_tool_config({**STATE, "claude_http_headers": {"x-team": "a"}}, MODEL)
    assert _blanks(env.managed) == BLANKS  # kept by the launch's rewrite

    claude.revert_managed_settings()
    assert not env.managed.exists()


def test_admin_passthrough_cannot_override_the_blanks(env):
    state = {
        **STATE,
        claude.SETTINGS_PASSTHROUGH_STATE_KEY: {
            "env": {"ANTHROPIC_API_KEY": "admin", "ANTHROPIC_AUTH_TOKEN": "admin", "OTHER": "ok"}
        },
    }
    claude.write_tool_config(state, MODEL)
    assert _blanks(env.user) == BLANKS
    assert _blanks(env.managed) == BLANKS
    assert _env(env.managed)["OTHER"] == "ok"
    claude.request_explicit_configure(False)
    claude.write_tool_config(state, MODEL)
    assert _blanks(env.managed) == BLANKS


def test_configure_keeps_a_value_someone_else_set_and_warns(env, capsys):
    env.managed.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "it-key", "ANTHROPIC_AUTH_TOKEN": ""}}),
        encoding="utf-8",
    )
    claude.write_tool_config(dict(STATE), MODEL)
    assert _env(env.managed)["ANTHROPIC_API_KEY"] == "it-key"
    assert _env(env.managed)["ANTHROPIC_AUTH_TOKEN"] == ""
    out = capsys.readouterr().out
    assert "env.ANTHROPIC_API_KEY" in out and "401" in out
    assert "env.ANTHROPIC_AUTH_TOKEN" not in out
    assert _blanks(env.user) == BLANKS  # ug's own file is still blanked
