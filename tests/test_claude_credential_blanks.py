import json
from types import SimpleNamespace

import pytest

from tests.test_claude_header_mod import _managed_env
from ucode.agents import claude

WS = "https://example.cloud.databricks.com"
BLANKS = {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}
# Spelled out, not taken from claude.CLAUDE_PROVIDER_ENV_KEYS, so a flag dropped there fails here.
ZEROS = dict.fromkeys(
    (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
        "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
        "CLAUDE_CODE_USE_MANTLE",
    ),
    "0",
)
# Every value ug pins so the shell or the user's settings can't take Claude off the gateway.
NEUTRAL = {**BLANKS, **ZEROS}
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


def _neutral(path):
    return {k: v for k, v in _env(path).items() if k in NEUTRAL}


def _write_managed_env(path, values):
    path.write_text(json.dumps({"env": values}), encoding="utf-8")


def _drop_neutral_like_a_second_writer(path):
    doc = json.loads(path.read_text())
    for key in NEUTRAL:
        doc["env"].pop(key)
    path.write_text(json.dumps(doc), encoding="utf-8")


STATE = {"workspace": WS, "codex_models": []}


def test_overlay_pins_neutral_values_for_helper_and_relayed_launches():
    for relayed in (False, True):
        overlay, keys = claude.render_overlay(
            WS,
            MODEL,
            {},
            relayed=relayed,
            relayed_base_url="http://127.0.0.1:9" if relayed else None,
        )
        assert {k: overlay["env"][k] for k in NEUTRAL} == NEUTRAL
        assert all(["env", k] in keys for k in NEUTRAL)
        # The gateway flag stays on; only the direct-provider flags are pinned off.
        assert overlay["env"]["CLAUDE_CODE_USE_GATEWAY"] == "1"


def test_configure_writes_neutral_values_to_both_files(env):
    claude.write_tool_config(dict(STATE), MODEL)
    assert _neutral(env.user) == NEUTRAL
    assert _neutral(env.managed) == NEUTRAL
    assert env.writes.count(str(env.managed)) == 1  # same privileged write as the rest of the file


def test_plain_launch_after_second_writer_dropped_them_makes_no_write(env):
    claude.write_tool_config(dict(STATE), MODEL)
    _drop_neutral_like_a_second_writer(env.managed)
    before = env.managed.read_bytes()
    count = len(env.writes)
    claude.request_explicit_configure(False)

    claude.write_tool_config(dict(STATE), MODEL)  # no conflict raised, no sudo
    claude.write_tool_config(dict(STATE), MODEL)

    assert len(env.writes) == count
    assert env.managed.read_bytes() == before
    assert _neutral(env.managed) == {}
    assert _neutral(env.user) == NEUTRAL


def test_plain_launch_does_not_add_neutral_values_to_a_fresh_managed_file(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert _neutral(env.managed) == {}
    assert _neutral(env.user) == NEUTRAL


def test_plain_launch_leaves_present_neutral_values_and_admin_values_alone(env):
    claude.write_tool_config(dict(STATE), MODEL)
    count = len(env.writes)
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert len(env.writes) == count
    assert _neutral(env.managed) == NEUTRAL

    # A value another party set is neither a conflict nor rewritten by a plain launch.
    doc = json.loads(env.managed.read_text())
    doc["env"]["ANTHROPIC_API_KEY"] = "admin-set"
    doc["env"]["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env.managed.write_text(json.dumps(doc), encoding="utf-8")
    claude.write_tool_config(dict(STATE), MODEL)
    assert len(env.writes) == count
    assert _env(env.managed)["ANTHROPIC_API_KEY"] == "admin-set"
    assert _env(env.managed)["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert _neutral(env.user) == NEUTRAL


def test_conflicts_ignore_the_neutral_values():
    existing = {
        "env": {"ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BASE_URL": "a"}
    }
    desired = {"env": {"ANTHROPIC_BASE_URL": "b"}}
    paths = [*claude.CLAUDE_NEUTRAL_ENV_PATHS, ["env", "ANTHROPIC_BASE_URL"]]
    assert claude._managed_settings_conflicts(existing, desired, paths) == [
        "env.ANTHROPIC_BASE_URL"
    ]


def test_relayed_launch_pins_user_file_and_leaves_managed_file_alone(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL, relayed=True)
    doc = json.loads(env.user.read_text())
    assert "apiKeyHelper" not in doc
    assert _neutral(env.user) == NEUTRAL
    assert not env.managed.exists()
    assert env.writes == []


def test_skip_mode_writes_only_the_user_file(env):
    env.mp.setenv("UG_SKIP_OS_SETTINGS", "1")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _neutral(env.user) == NEUTRAL
    assert not env.managed.exists()
    assert env.writes == []


def test_revert_removes_neutral_values_from_the_managed_file(env):
    _write_managed_env(env.managed, {"KEEP": "1"})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _neutral(env.managed) == NEUTRAL

    claude.revert_managed_settings()
    assert json.loads(env.managed.read_text()) == {"env": {"KEEP": "1"}}


def test_revert_removes_neutral_values_added_by_configure_after_a_launch_rewrite(env):
    claude.write_tool_config(dict(STATE), MODEL)
    claude.request_explicit_configure(False)
    claude.write_tool_config({**STATE, "claude_http_headers": {"x-team": "a"}}, MODEL)
    assert _neutral(env.managed) == NEUTRAL  # kept by the launch's rewrite

    claude.revert_managed_settings()
    assert not env.managed.exists()


def test_admin_passthrough_cannot_override_the_neutral_values(env):
    state = {
        **STATE,
        claude.SETTINGS_PASSTHROUGH_STATE_KEY: {
            "env": {
                "ANTHROPIC_API_KEY": "admin",
                "ANTHROPIC_AUTH_TOKEN": "admin",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "OTHER": "ok",
            }
        },
    }
    claude.write_tool_config(state, MODEL)
    assert _neutral(env.user) == NEUTRAL
    assert _neutral(env.managed) == NEUTRAL
    assert _env(env.managed)["OTHER"] == "ok"
    claude.request_explicit_configure(False)
    claude.write_tool_config(state, MODEL)
    assert _neutral(env.managed) == NEUTRAL


def test_configure_keeps_a_credential_someone_else_set_and_warns(env, capsys):
    _write_managed_env(env.managed, {"ANTHROPIC_API_KEY": "it-key", "ANTHROPIC_AUTH_TOKEN": ""})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _env(env.managed)["ANTHROPIC_API_KEY"] == "it-key"
    assert _env(env.managed)["ANTHROPIC_AUTH_TOKEN"] == ""
    out = capsys.readouterr().out
    assert "env.ANTHROPIC_API_KEY" in out and "401" in out
    assert "bypasses the gateway" not in out  # the provider-flag warning text is not used here
    assert "env.ANTHROPIC_AUTH_TOKEN" not in out
    assert _neutral(env.user) == NEUTRAL  # ug's own file is still pinned


# Provider flags are classified by Claude Code's truthy rule, not by "non-empty" like the
# credentials, so a false value an admin left is replaced instead of kept.


@pytest.mark.parametrize("value", ["1", "true", " Yes ", "ON"])
def test_configure_keeps_a_true_provider_flag_someone_else_set_and_warns(env, capsys, value):
    _write_managed_env(env.managed, {"CLAUDE_CODE_USE_BEDROCK": value})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _env(env.managed)["CLAUDE_CODE_USE_BEDROCK"] == value
    assert _env(env.managed)["CLAUDE_CODE_USE_VERTEX"] == "0"
    out = capsys.readouterr().out
    assert "env.CLAUDE_CODE_USE_BEDROCK" in out and "bypasses the gateway" in out
    assert "env.CLAUDE_CODE_USE_VERTEX" not in out
    assert _neutral(env.user) == NEUTRAL  # ug's own file still pins it off


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_configure_replaces_a_false_provider_flag_without_warning(env, capsys, value):
    _write_managed_env(env.managed, {"CLAUDE_CODE_USE_BEDROCK": value})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _neutral(env.managed) == NEUTRAL
    assert "CLAUDE_CODE_USE_BEDROCK" not in capsys.readouterr().out
