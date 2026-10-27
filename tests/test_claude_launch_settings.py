"""Cross-platform launch settings lifetime, with process boundaries controlled."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ucode import gateway_proxy, launcher
from ucode.agents import LaunchOptions, claude
from ucode.managed_files import OS

PINNED_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture(params=[OS.LINUX, OS.MACOS, OS.WINDOWS])
def large_settings(tmp_path, monkeypatch, request):
    path = tmp_path / "saved settings.json"
    settings = {
        "availableModels": [f"catalog.schema.claude-model-{i:04d}-{'x' * 32}" for i in range(650)],
        "env": {"USER_SETTING": "preserve me"},
        "permissions": {"deny": ["Read(private/**)"]},
    }
    path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS_PATH", path)
    monkeypatch.setattr(claude, "current_os", lambda: request.param)
    monkeypatch.setattr(
        launcher,
        "os",
        SimpleNamespace(
            name="nt" if request.param == OS.WINDOWS else "posix",
            execvp=lambda *args: pytest.fail("temporary settings need parent-process cleanup"),
        ),
    )
    monkeypatch.setattr(
        claude.shutil, "which", lambda name: str(tmp_path / "Claude Code" / "claude.exe")
    )
    return path, settings


@pytest.mark.parametrize("outcome", [0, 7, "interrupt", "spawn-error"])
def test_pinned_model_keeps_settings_until_process_exits(large_settings, monkeypatch, outcome):
    saved_path, original = large_settings
    saved_bytes = saved_path.read_bytes()
    seen = []
    signals = []
    prompt = 'Read "résumé.txt" & keep | %PATH% literal'

    class Process:
        waits = 0

        def wait(self):
            assert seen[0].exists()
            self.waits += 1
            if outcome == "interrupt" and self.waits == 1:
                raise KeyboardInterrupt
            return 7 if outcome == "interrupt" else outcome

        def send_signal(self, value):
            signals.append(value)

    def popen(argv, **kwargs):
        if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
            raise OSError("[WinError 206] The filename or extension is too long")
        settings_path = Path(argv[argv.index("--settings") + 1])
        seen.append(settings_path)
        assert settings_path != saved_path
        merged = json.loads(settings_path.read_text(encoding="utf-8"))
        assert merged == {**original, "env": {**original["env"], "ANTHROPIC_MODEL": PINNED_MODEL}}
        assert argv[-2:] == ["--print", prompt]
        assert argv.count("--settings") == 1
        if outcome == "spawn-error":
            raise OSError("could not start Claude")
        return Process()

    monkeypatch.setattr(claude.subprocess_cross_os, "popen", popen)
    error_type = OSError if outcome == "spawn-error" else SystemExit
    with pytest.raises(error_type) as exc:
        claude.launch(
            {}, ["--print", prompt], options=LaunchOptions(user_pinned_model=PINNED_MODEL)
        )
    if outcome == "spawn-error":
        assert "could not start Claude" in str(exc.value)
    else:
        assert exc.value.code == (7 if outcome == "interrupt" else outcome)
    assert signals == ([signal.SIGINT] if outcome == "interrupt" else [])
    assert len(seen) == 1
    assert not seen[0].parent.exists()
    assert saved_path.read_bytes() == saved_bytes


@pytest.mark.parametrize("spawn_failure", [False, True])
def test_relayed_settings_and_proxy_are_cleaned_up(large_settings, monkeypatch, spawn_failure):
    saved_path, original = large_settings
    saved_bytes = saved_path.read_bytes()
    server = SimpleNamespace(
        server_address=("127.0.0.1", 12345), serve_forever=lambda: None, shutdown=Mock()
    )
    cache = SimpleNamespace(stop=Mock())
    client = SimpleNamespace(close=Mock())
    monkeypatch.setattr(claude, "_ensure_subscription_login", lambda: None)
    monkeypatch.setattr(gateway_proxy, "start_relay_proxy", lambda *args: (server, cache, client))
    seen = []
    caller = {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo caller"}]}]}
    }

    def popen(argv, **kwargs):
        settings_path = Path(argv[argv.index("--settings") + 1])
        seen.append(settings_path)
        merged = json.loads(settings_path.read_text(encoding="utf-8"))
        assert merged == {**original, **caller}
        assert argv[1:3] == ["--setting-sources", "project,local"]
        if spawn_failure:
            raise OSError("could not start Claude")

        def wait():
            assert settings_path.exists()
            return 3

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(claude.subprocess_cross_os, "popen", popen)
    with pytest.raises(OSError if spawn_failure else SystemExit) as exc:
        claude.launch(
            {
                "workspace": "https://example.databricks.com",
                "claude_relayed": True,
                "relayed_proxy_port": 12345,
            },
            ["--settings", json.dumps(caller), "--print", "hello"],
            options=LaunchOptions(),
        )
    if not spawn_failure:
        assert exc.value.code == 3
    assert len(seen) == 1
    assert not seen[0].parent.exists()
    cache.stop.assert_called_once()
    server.shutdown.assert_called_once()
    client.close.assert_called_once()
    assert saved_path.read_bytes() == saved_bytes


@pytest.mark.parametrize("payload_kind", ["large-catalog", "unicode-prompt"])
def test_spilled_settings_are_readable_by_a_real_child(large_settings, payload_kind):
    saved_path, original = large_settings
    prompt = 'Read "résumé.txt" & keep | %PATH% literal'
    if payload_kind == "unicode-prompt":
        original = {"env": {"USER_SETTING": "p" * 18_000}}
        saved_path.write_text(json.dumps(original), encoding="utf-8")
        prompt = "😀" * 8_000
    child = (
        "import json, pathlib, sys; "
        "settings = json.loads(pathlib.Path(sys.argv[sys.argv.index('--settings') + 1]).read_text(encoding='utf-8')); "
        "assert settings['env']['ANTHROPIC_MODEL'] == sys.argv[-3]; "
        "assert settings['env']['USER_SETTING']; "
        "print(len(settings.get('availableModels', [])), len(sys.argv[-1]))"
    )
    with claude._claude_launch_argv(
        sys.executable,
        ["--model", PINNED_MODEL, "--print", prompt],
        settings_override={"env": {"ANTHROPIC_MODEL": PINNED_MODEL}},
    ) as argv:
        settings_path = Path(argv[argv.index("--settings") + 1])
        assert settings_path.is_file() and settings_path != saved_path
        result = subprocess.run(
            [sys.executable, "-c", child, *argv[1:]],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert result.stdout.strip() == f"{len(original.get('availableModels', []))} {len(prompt)}"
    assert not settings_path.parent.exists()


def test_simultaneous_launches_keep_separate_settings(large_settings):
    saved_path, _ = large_settings
    saved_bytes = saved_path.read_bytes()
    with claude._claude_launch_argv(
        "claude", [], settings_override={"env": {"ANTHROPIC_MODEL": "first-model"}}
    ) as first:
        first_path = Path(first[first.index("--settings") + 1])
        with claude._claude_launch_argv(
            "claude", [], settings_override={"env": {"ANTHROPIC_MODEL": "second-model"}}
        ) as second:
            second_path = Path(second[second.index("--settings") + 1])
            assert first_path != second_path
            assert (
                json.loads(first_path.read_text(encoding="utf-8"))["env"]["ANTHROPIC_MODEL"]
                == "first-model"
            )
            assert (
                json.loads(second_path.read_text(encoding="utf-8"))["env"]["ANTHROPIC_MODEL"]
                == "second-model"
            )
        assert first_path.is_file()
        assert not second_path.parent.exists()
    assert not first_path.parent.exists()
    assert saved_path.read_bytes() == saved_bytes


def test_caller_settings_use_private_file(large_settings):
    _, original = large_settings
    with claude._claude_launch_argv("claude", ["--settings", "{}", "--print", "hello"]) as argv:
        settings_path = Path(argv[argv.index("--settings") + 1])
        assert settings_path.name == "settings.json"
        assert json.loads(settings_path.read_text(encoding="utf-8")) == original
        if sys.platform != "win32":
            assert settings_path.stat().st_mode & 0o777 == 0o600
        assert argv[-2:] == ["--print", "hello"]
    assert not settings_path.parent.exists()


def test_small_settings_also_use_file(large_settings):
    saved_path, _ = large_settings
    saved_path.write_text('{"env":{"USER_SETTING":"keep"}}', encoding="utf-8")
    with claude._claude_launch_argv(
        "claude", [], settings_override={"env": {"ANTHROPIC_MODEL": PINNED_MODEL}}
    ) as argv:
        settings_path = Path(argv[argv.index("--settings") + 1])
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "env": {"USER_SETTING": "keep", "ANTHROPIC_MODEL": PINNED_MODEL}
        }
    assert not settings_path.parent.exists()


def test_saved_settings_are_reused_without_overrides(large_settings):
    saved_path, _ = large_settings
    saved_bytes = saved_path.read_bytes()
    with claude._claude_launch_argv("claude", ["--print", "hello"]) as argv:
        assert argv == ["claude", "--settings", str(saved_path), "--print", "hello"]
    assert saved_path.read_bytes() == saved_bytes
