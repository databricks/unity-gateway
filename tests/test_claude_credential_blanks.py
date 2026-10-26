"""ug blanks ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY so a shell credential can't outrank its helper.

The user file always has the blanks; the managed file gets them from `ug configure` only, like the
header mod, so a second writer of that file that drops them is not fought on every launch (CUJ 8).
"""

import json
import shutil
from types import SimpleNamespace

import pytest

from ucode import managed_files
from ucode.agents import claude
from ucode.managed_files import OS

WS = "https://example.cloud.databricks.com"
BLANKS = {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""}
MODEL = "databricks-claude-sonnet-4"


@pytest.fixture
def env(tmp_path, monkeypatch):
    managed_path = tmp_path / "managed-settings.json"
    backup_dir = tmp_path / "managed-backups"
    writes: list[str] = []
    monkeypatch.setattr(managed_files, "managed_files_supported", lambda: True)
    monkeypatch.setattr(managed_files, "current_os", lambda: OS.LINUX)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(managed_files, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json")

    def _write(target, text):
        writes.append(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(managed_files, "_sudo_replace", _write)
    monkeypatch.setattr(managed_files, "_sudo_remove", lambda path: path.unlink())
    monkeypatch.setattr(claude, "remove_privileged_tree", shutil.rmtree)
    monkeypatch.setattr(claude, "_managed_settings_path", lambda: managed_path)
    monkeypatch.setattr(claude, "claude_header_mod_root", lambda: None)
    monkeypatch.setattr(claude, "managed_writes_allowed", lambda: True)
    monkeypatch.setattr(managed_files.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(claude, "backup_existing_file", lambda *a, **kw: True)
    monkeypatch.setattr(claude, "save_state", lambda state: None)
    monkeypatch.setattr(claude, "ug_version", lambda: "1.0")
    monkeypatch.setattr(claude, "agent_version", lambda _binary: "2.0")
    user_path = tmp_path / "ucode-settings.json"
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", user_path)
    monkeypatch.setattr(
        claude, "refresh_managed_config", lambda *a, **kw: SimpleNamespace(manifest={"claude": {}})
    )
    monkeypatch.setattr(claude, "_header_mod_requested", True)  # as `ug configure` runs it
    monkeypatch.delenv("UG_HEADERS", raising=False)
    return SimpleNamespace(managed=managed_path, user=user_path, writes=writes, mp=monkeypatch)


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
    assert len(env.writes) == 1  # same privileged write as the rest of the file


def test_plain_launch_after_second_writer_dropped_them_makes_no_write(env):
    claude.write_tool_config(dict(STATE), MODEL)
    _drop_blanks_like_a_second_writer(env.managed)
    before = env.managed.read_bytes()
    count = len(env.writes)
    env.mp.setattr(claude, "_header_mod_requested", False)

    claude.write_tool_config(dict(STATE), MODEL)  # no conflict raised, no sudo
    claude.write_tool_config(dict(STATE), MODEL)

    assert len(env.writes) == count
    assert env.managed.read_bytes() == before
    assert _blanks(env.managed) == {}
    assert _blanks(env.user) == BLANKS


def test_plain_launch_does_not_add_blanks_to_a_fresh_managed_file(env):
    env.mp.setattr(claude, "_header_mod_requested", False)
    claude.write_tool_config(dict(STATE), MODEL)
    assert _blanks(env.managed) == {}
    assert _blanks(env.user) == BLANKS


def test_plain_launch_leaves_present_blanks_and_values_alone(env):
    claude.write_tool_config(dict(STATE), MODEL)
    count = len(env.writes)
    env.mp.setattr(claude, "_header_mod_requested", False)
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
    existing = {"env": {"ANTHROPIC_API_KEY": "x"}}
    desired = {"env": {}}
    assert (
        claude._managed_settings_conflicts(
            existing, desired, claude.CLAUDE_BLANKED_CREDENTIAL_PATHS
        )
        == []
    )


def test_relayed_launch_blanks_user_file_and_leaves_managed_file_alone(env):
    env.mp.setattr(claude, "_header_mod_requested", False)
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
    env.mp.setattr(claude, "_header_mod_requested", False)
    claude.write_tool_config({**STATE, "claude_http_headers": {"x-team": "a"}}, MODEL)
    assert _blanks(env.managed) == BLANKS  # kept by the launch's rewrite

    claude.revert_managed_settings()
    assert not env.managed.exists() or _blanks(env.managed) == {}


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
    env.mp.setattr(claude, "_header_mod_requested", False)
    claude.write_tool_config(state, MODEL)
    assert _blanks(env.managed) == BLANKS
