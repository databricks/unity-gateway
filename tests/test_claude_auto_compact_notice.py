"""Tests for the `/autocompact` restart notice (hook, launch record, and hook install)."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from ucode import claude_auto_compact_notice as notice
from ucode.agents import claude
from ucode.auto_compact_window import GPT_AUTO_COMPACT_WINDOW

MODEL = "system.ai.gpt-6-luna"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(notice.Path, "home", lambda: home)
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", home / ".claude" / "ucode-settings.json")
    monkeypatch.setattr(claude, "CLAUDE_USER_SETTINGS_PATH", home / ".claude" / "settings.json")
    monkeypatch.setattr(claude, "managed_settings_path", lambda: None)
    monkeypatch.delenv(notice.SESSION_WINDOWS_ENV_VAR, raising=False)
    return home


def _user_file(home, windows: dict) -> bytes:
    path = home / ".claude" / "settings.json"
    path.write_text(
        json.dumps({"modelSettings": {m: {"autoCompactWindow": w} for m, w in windows.items()}})
    )
    return path.read_bytes()


def _session(monkeypatch, effective, user=None, model=MODEL):
    monkeypatch.setenv(
        notice.SESSION_WINDOWS_ENV_VAR,
        json.dumps({model: {"effective": effective, "user": user}}),
    )


def _run_hook(monkeypatch, capsys, session_id="s1"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session_id})))
    assert notice.main() == 0
    return capsys.readouterr().out


class TestNoticeFor:
    def test_lower_value_needs_restart(self):
        assert notice.notice_for(MODEL, 800_000, None, 500_000) == (
            f"Auto-compact for {MODEL} changes to 500k after you restart ug claude."
        )

    def test_over_cap_with_unchanged_effective_does_not_mention_restart(self):
        message = notice.notice_for(MODEL, GPT_AUTO_COMPACT_WINDOW, None, 900_000)
        assert message == (
            f"Auto-compact for {MODEL} stays at 800k: ug caps it because the gateway rejects "
            "requests above ~920K tokens."
        )

    def test_over_cap_that_changes_effective_mentions_restart(self):
        message = notice.notice_for(MODEL, 500_000, 500_000, 900_000)
        assert message is not None
        assert "stays at 800k" in message
        assert message.endswith("Restart ug claude to apply the cap.")

    @pytest.mark.parametrize(
        ("effective", "launch_user", "user"),
        [
            (800_000, None, None),  # no user value
            (500_000, 500_000, 500_000),  # equal to the effective value
            (800_000, 800_000, 800_000),  # equal at the cap
            (800_000, 900_000, 900_000),  # standing over-cap value ug already warned about
            (500_000, 500_000, None),  # value removed
        ],
    )
    def test_silent(self, effective, launch_user, user):
        assert notice.notice_for(MODEL, effective, launch_user, user) is None


class TestUserWindows:
    def test_invalid_values_are_ignored(self):
        settings = {
            "modelSettings": {
                "a": {"autoCompactWindow": 99_999},
                "b": {"autoCompactWindow": 1_000_001},
                "c": {"autoCompactWindow": "500000"},
                "d": {"autoCompactWindow": True},
                "e": {"autoCompactWindow": 500_000.0},
                "f": "oops",
                "g": {"autoCompactWindow": 300_000},
            }
        }
        assert notice.user_auto_compact_windows(settings) == {"g": 300_000}

    def test_lower_spelling_wins(self):
        settings = {
            "modelSettings": {
                MODEL: {"autoCompactWindow": 600_000},
                f"{MODEL}[1m]": {"autoCompactWindow": 400_000},
            }
        }
        assert notice.user_auto_compact_windows(settings) == {MODEL: 400_000}

    @pytest.mark.parametrize("settings", [None, [], {}, {"modelSettings": []}])
    def test_malformed_documents(self, settings):
        assert notice.user_auto_compact_windows(settings) == {}


class TestHook:
    def test_shows_each_notice_once_per_session(self, _isolated, monkeypatch, capsys):
        _session(monkeypatch, 800_000)
        original = _user_file(_isolated, {MODEL: 500_000})
        first = json.loads(_run_hook(monkeypatch, capsys))
        assert first == {
            "systemMessage": f"Auto-compact for {MODEL} changes to 500k after you restart ug claude."
        }
        assert _run_hook(monkeypatch, capsys) == ""
        assert (_isolated / ".claude" / "settings.json").read_bytes() == original

    def test_new_value_and_new_session_show_again(self, _isolated, monkeypatch, capsys):
        _session(monkeypatch, 800_000)
        _user_file(_isolated, {MODEL: 500_000})
        assert _run_hook(monkeypatch, capsys) != ""
        _user_file(_isolated, {MODEL: 600_000})
        assert "600k" in _run_hook(monkeypatch, capsys)
        _user_file(_isolated, {MODEL: 500_000})
        assert _run_hook(monkeypatch, capsys) == ""
        assert _run_hook(monkeypatch, capsys, session_id="s2") != ""

    @pytest.mark.parametrize(
        "user_windows",
        [
            {},  # no user value
            {MODEL: 800_000},  # equal to the effective value
            {"system.ai.claude-opus-4-8": 300_000},  # not a recorded (GPT) id
            {MODEL: 50_000},  # invalid
            {MODEL: "huge"},  # invalid
        ],
    )
    def test_silent_cases(self, _isolated, monkeypatch, capsys, user_windows):
        _session(monkeypatch, 800_000)
        original = _user_file(_isolated, user_windows)
        assert _run_hook(monkeypatch, capsys) == ""
        assert (_isolated / ".claude" / "settings.json").read_bytes() == original

    def test_silent_without_a_recorded_session(self, _isolated, monkeypatch, capsys):
        _user_file(_isolated, {MODEL: 500_000})
        assert _run_hook(monkeypatch, capsys) == ""

    def test_missing_user_file_is_silent_and_not_created(self, _isolated, monkeypatch, capsys):
        _session(monkeypatch, 800_000)
        assert _run_hook(monkeypatch, capsys) == ""
        assert not (_isolated / ".claude" / "settings.json").exists()

    @pytest.mark.parametrize("stdin", ["", "not json", "[]", json.dumps({"session_id": "../x"})])
    def test_bad_input_never_fails(self, _isolated, monkeypatch, capsys, stdin):
        _session(monkeypatch, 800_000)
        _user_file(_isolated, {MODEL: 500_000})
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert notice.main() == 0
        assert capsys.readouterr().out == ""

    def test_bad_user_file_or_env_never_fails(self, _isolated, monkeypatch, capsys):
        _session(monkeypatch, 800_000)
        (_isolated / ".claude" / "settings.json").write_text("{broken")
        assert _run_hook(monkeypatch, capsys) == ""
        monkeypatch.setenv(notice.SESSION_WINDOWS_ENV_VAR, "{broken")
        assert _run_hook(monkeypatch, capsys) == ""

    def test_runs_as_module_and_exits_zero(self, _isolated, monkeypatch):
        _session(monkeypatch, 800_000)
        _user_file(_isolated, {MODEL: 500_000})
        result = subprocess.run(
            [sys.executable, "-m", "ucode.claude_auto_compact_notice"],
            input=json.dumps({"session_id": "s9"}),
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "HOME": str(_isolated)},
            check=False,
        )
        assert result.returncode == 0
        assert "changes to 500k" in json.loads(result.stdout)["systemMessage"]


class TestLaunchRecord:
    def _recorded(self, monkeypatch):
        import os

        raw = os.environ.get(notice.SESSION_WINDOWS_ENV_VAR)
        return json.loads(raw) if raw else None

    def test_records_effective_and_launch_user_value(self, _isolated, monkeypatch):
        claude.CLAUDE_SETTINGS_PATH.write_text(
            json.dumps({"modelSettings": {MODEL: {"autoCompactWindow": 500_000}}})
        )
        original = _user_file(_isolated, {f"{MODEL}[1m]": 900_000})
        claude.record_session_gpt_auto_compact()
        assert self._recorded(monkeypatch) == {MODEL: {"effective": 500_000, "user": 900_000}}
        assert claude.CLAUDE_USER_SETTINGS_PATH.read_bytes() == original

    def test_launch_overlay_and_managed_file_take_precedence(self, _isolated, monkeypatch):
        managed = _isolated / "managed-settings.json"
        managed.write_text(json.dumps({"modelSettings": {MODEL: {"autoCompactWindow": 300_000}}}))
        claude.CLAUDE_SETTINGS_PATH.write_text(
            json.dumps({"modelSettings": {MODEL: {"autoCompactWindow": 500_000}}})
        )
        overlay = {"modelSettings": {MODEL: {"autoCompactWindow": 700_000}}}
        claude.record_session_gpt_auto_compact(overlay)
        assert self._recorded(monkeypatch)[MODEL]["effective"] == 700_000
        monkeypatch.setattr(claude, "managed_settings_path", lambda: managed)
        claude.record_session_gpt_auto_compact(overlay)
        assert self._recorded(monkeypatch)[MODEL]["effective"] == 300_000

    def test_non_gpt_windows_are_not_recorded_and_stale_env_is_cleared(
        self, _isolated, monkeypatch
    ):
        monkeypatch.setenv(notice.SESSION_WINDOWS_ENV_VAR, '{"x": {"effective": 200000}}')
        claude.CLAUDE_SETTINGS_PATH.write_text(
            json.dumps({"modelSettings": {"system.ai.claude-opus-4-8": {"autoCompactWindow": 4e5}}})
        )
        claude.record_session_gpt_auto_compact()
        assert self._recorded(monkeypatch) is None


class TestHookInstall:
    def test_installs_one_marked_user_prompt_submit_hook_and_keeps_user_hooks(self):
        user_hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
        doc = {"hooks": {"UserPromptSubmit": [user_hook]}}
        claude.sync_auto_compact_notice_hook(doc)
        claude.sync_auto_compact_notice_hook(doc)
        groups = doc["hooks"]["UserPromptSubmit"]
        assert groups[0] == user_hook
        assert len(groups) == 2
        command = groups[1]["hooks"][0]["command"]
        assert command.startswith(f"{sys.executable} -m ") or sys.executable in command
        assert notice.HOOK_COMMAND_MARKER in command
