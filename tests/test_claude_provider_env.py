import json
from types import SimpleNamespace

import pytest

from tests.test_claude_header_mod import _managed_env
from ucode.agents import claude

WS = "https://example.cloud.databricks.com"
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
MODEL = "databricks-claude-sonnet-4"
STATE = {"workspace": WS, "codex_models": []}


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


def _zeros(path):
    return {k: v for k, v in _env(path).items() if k in ZEROS}


def _write_managed_env(path, values):
    path.write_text(json.dumps({"env": values}), encoding="utf-8")


def test_overlay_zeroes_every_provider_flag_for_helper_and_relayed_launches():
    for relayed in (False, True):
        overlay, keys = claude.render_overlay(
            WS,
            MODEL,
            {},
            relayed=relayed,
            relayed_base_url="http://127.0.0.1:9" if relayed else None,
        )
        assert {k: overlay["env"][k] for k in ZEROS} == ZEROS
        assert all(["env", k] in keys for k in ZEROS)
        # The gateway flag stays on; only the direct-provider flags are pinned off.
        assert overlay["env"]["CLAUDE_CODE_USE_GATEWAY"] == "1"


def test_configure_writes_zeros_to_both_files(env):
    claude.write_tool_config(dict(STATE), MODEL)
    assert _zeros(env.user) == ZEROS
    assert _zeros(env.managed) == ZEROS
    assert env.writes.count(str(env.managed)) == 1  # same privileged write as the rest of the file


def test_plain_launch_does_not_add_zeros_to_a_fresh_managed_file(env):
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert _zeros(env.managed) == {}
    assert _zeros(env.user) == ZEROS


def test_plain_launch_after_second_writer_dropped_them_makes_no_write(env):
    claude.write_tool_config(dict(STATE), MODEL)
    doc = json.loads(env.managed.read_text())
    for key in ZEROS:
        doc["env"].pop(key)
    env.managed.write_text(json.dumps(doc), encoding="utf-8")
    before = env.managed.read_bytes()
    count = len(env.writes)
    claude.request_explicit_configure(False)

    claude.write_tool_config(dict(STATE), MODEL)  # no conflict raised, no sudo

    assert len(env.writes) == count
    assert env.managed.read_bytes() == before
    assert _zeros(env.user) == ZEROS


@pytest.mark.parametrize("value", ["1", "true", " Yes ", "ON"])
def test_configure_keeps_a_true_flag_someone_else_set_and_warns(env, capsys, value):
    _write_managed_env(env.managed, {"CLAUDE_CODE_USE_BEDROCK": value})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _env(env.managed)["CLAUDE_CODE_USE_BEDROCK"] == value
    assert _env(env.managed)["CLAUDE_CODE_USE_VERTEX"] == "0"
    out = capsys.readouterr().out
    assert "env.CLAUDE_CODE_USE_BEDROCK" in out and "bypasses the gateway" in out
    assert "env.CLAUDE_CODE_USE_VERTEX" not in out
    assert _zeros(env.user) == ZEROS  # ug's own file still pins it off


@pytest.mark.parametrize("value", ["0", "false", ""])
def test_configure_replaces_a_false_flag_without_warning(env, capsys, value):
    _write_managed_env(env.managed, {"CLAUDE_CODE_USE_BEDROCK": value})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _zeros(env.managed) == ZEROS
    assert "CLAUDE_CODE_USE_BEDROCK" not in capsys.readouterr().out


def test_plain_launch_leaves_a_true_flag_in_the_managed_file_alone(env):
    _write_managed_env(env.managed, {"CLAUDE_CODE_USE_BEDROCK": "1"})
    claude.request_explicit_configure(False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert _env(env.managed)["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert _zeros(env.user) == ZEROS


def test_credential_warning_is_unchanged(env, capsys):
    _write_managed_env(env.managed, {"ANTHROPIC_API_KEY": "it-key"})
    claude.write_tool_config(dict(STATE), MODEL)
    out = capsys.readouterr().out
    assert "env.ANTHROPIC_API_KEY" in out and "401" in out
    assert "bypasses the gateway" not in out


def test_conflicts_ignore_the_provider_flags():
    existing = {"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BASE_URL": "a"}}
    desired = {"env": {"ANTHROPIC_BASE_URL": "b"}}
    paths = [*claude.CLAUDE_NEUTRAL_ENV_PATHS, ["env", "ANTHROPIC_BASE_URL"]]
    assert claude._managed_settings_conflicts(existing, desired, paths) == [
        "env.ANTHROPIC_BASE_URL"
    ]


def test_skip_mode_writes_only_the_user_file(env):
    env.mp.setenv("UG_SKIP_OS_SETTINGS", "1")
    claude.write_tool_config(dict(STATE), MODEL)
    assert _zeros(env.user) == ZEROS
    assert not env.managed.exists()
    assert env.writes == []


def test_revert_removes_zeros_from_the_managed_file(env):
    _write_managed_env(env.managed, {"KEEP": "1"})
    claude.write_tool_config(dict(STATE), MODEL)
    assert _zeros(env.managed) == ZEROS

    claude.revert_managed_settings()
    assert json.loads(env.managed.read_text()) == {"env": {"KEEP": "1"}}


def test_admin_passthrough_cannot_turn_a_provider_flag_on(env):
    state = {
        **STATE,
        claude.SETTINGS_PASSTHROUGH_STATE_KEY: {
            "env": {"CLAUDE_CODE_USE_BEDROCK": "1", "OTHER": "ok"}
        },
    }
    claude.write_tool_config(state, MODEL)
    assert _zeros(env.user) == ZEROS
    assert _zeros(env.managed) == ZEROS
    assert _env(env.managed)["OTHER"] == "ok"
