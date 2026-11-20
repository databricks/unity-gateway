"""The GitHub Copilot app's own trace export to the gateway: the login-profile block (macOS,
Linux), the user environment (Windows), and ``telemetry.headersProvider`` in Copilot's settings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ucode.agents.desktop import copilot_app_plugin as plugin
from ucode.agents.desktop import copilot_app_tracing as tracing
from ucode.agents.desktop import shell_profile_env
from ucode.managed_files import OS
from ucode.os_compatibility import windows_user_env

WS = "https://example.cloud.databricks.com"
_REAL_CACHE_DIR = tracing._cache_dir
ENV = tracing.app_env(WS)
PROVIDER = tracing.headers_provider("/opt/ug/bin/ug", ["otel-headers", "--host", WS])


@pytest.fixture
def home():
    return shell_profile_env._home()


def _settings_path() -> Path:
    return plugin.copilot_settings_path()


def _settings() -> dict:
    return json.loads(_settings_path().read_text())


def _write_settings(settings: dict) -> None:
    _settings_path().parent.mkdir(parents=True, exist_ok=True)
    _settings_path().write_text(json.dumps(settings))


def _out(capsys) -> str:
    return " ".join(capsys.readouterr().out.split())


def test_app_env_exports_only_traces_to_the_gateway_as_protobuf():
    assert tracing.app_env(WS + "/") == {
        "COPILOT_OTEL_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{WS}/ai-gateway/otel/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
    }
    assert PROVIDER == {
        "path": "/opt/ug/bin/ug",
        "args": ["otel-headers", "--host", WS],
        "refreshIntervalMs": 1_500_000,
    }


class TestProfileFiles:
    def test_bash_uses_the_first_login_file_that_exists(self, home):
        home.mkdir(parents=True)
        assert shell_profile_env.profile_file("bash") == home / ".profile"
        (home / ".bash_login").write_text("")
        assert shell_profile_env.profile_file("bash") == home / ".bash_login"
        (home / ".bash_profile").write_text("")
        assert shell_profile_env.profile_file("bash") == home / ".bash_profile"

    def test_zsh_sh_pwsh_and_fish(self, home, monkeypatch):
        assert shell_profile_env.profile_file("zsh") == home / ".zshenv"
        for shell in ("sh", "dash", "pwsh"):
            assert shell_profile_env.profile_file(shell) == home / ".profile"
        assert shell_profile_env.profile_file("fish") == (
            home / ".config" / "fish" / "conf.d" / "unity-gateway.fish"
        )
        monkeypatch.setattr(shell_profile_env, "_system_zdotdir", lambda: "/etc/zdot")
        assert shell_profile_env.profile_file("zsh") == Path("/etc/zdot/.zshenv")

    @pytest.mark.parametrize("shell", ["ksh", "mksh", "elvish", "tcsh"])
    def test_shells_the_app_doesnt_probe(self, shell):
        assert shell_profile_env.profile_file(shell) is None


class TestProfileBlock:
    def test_write_is_idempotent_and_remove_restores_the_file(self, tmp_path):
        path = tmp_path / ".zshenv"
        original = "export EDITOR=vim\n"
        path.write_text(original)

        assert shell_profile_env.write(path, ENV)
        assert not shell_profile_env.write(path, ENV)
        text = path.read_text()
        assert text.startswith(original)
        assert text.count(shell_profile_env.BEGIN) == 1
        assert 'if [ -n "${COPILOT_RESOLVING_ENVIRONMENT:-}" ]; then' in text
        assert "OTEL_EXPORTER_OTLP_ENDPOINT=" not in text

        assert shell_profile_env.write(path, tracing.app_env("https://other.example.com"))
        assert path.read_text().count(shell_profile_env.BEGIN) == 1

        assert shell_profile_env.remove(path)
        assert path.read_text() == original
        assert not shell_profile_env.remove(path)

    def test_goes_first_in_a_profile_that_can_return_early(self, tmp_path):
        path = tmp_path / ".profile"
        path.write_text('[ -z "$PS1" ] && return\nexport A=1\n')

        shell_profile_env.write(path, ENV)

        assert path.read_text().startswith(shell_profile_env.BEGIN)
        shell_profile_env.remove(path)
        assert path.read_text() == '[ -z "$PS1" ] && return\nexport A=1\n'

    def test_fish_gets_its_own_file_which_remove_deletes(self, home):
        path = shell_profile_env.profile_file("fish")
        assert path is not None

        shell_profile_env.write(path, {"A": "it's"})

        assert "if set -q COPILOT_RESOLVING_ENVIRONMENT" in path.read_text()
        assert "set -gx A 'it\\'s'" in path.read_text()
        shell_profile_env.remove(path)
        assert not path.exists()

    @pytest.mark.parametrize("shell", ["bash", "sh", "fish"])
    def test_only_the_apps_probe_sees_the_variables(self, shell, home):
        binary = shutil.which(shell)
        if binary is None or sys.platform == "win32":
            pytest.skip(f"{shell} isn't installed")
        path = shell_profile_env.profile_file(shell)
        assert path is not None
        shell_profile_env.write(path, ENV)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "XDG_CONFIG_HOME": str(home / ".config")}

        def probe(extra: dict) -> str:
            return subprocess.run(
                [binary, "-l", "-c", "echo $OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
                env={**env, **extra},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            ).stdout.strip()

        assert (
            probe({"COPILOT_RESOLVING_ENVIRONMENT": "1"})
            == ENV["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
        )
        assert probe({}) == ""


class TestHeadersProviderSetting:
    def test_merges_only_headers_provider_and_keeps_the_rest(self):
        _write_settings({"model": "m", "telemetry": {"capture": True, "captureContent": False}})

        assert plugin.set_headers_provider(PROVIDER)
        assert not plugin.set_headers_provider(PROVIDER)

        assert _settings() == {
            "model": "m",
            "telemetry": {"capture": True, "captureContent": False, "headersProvider": PROVIDER},
        }
        assert plugin.remove_headers_provider()
        assert _settings() == {
            "model": "m",
            "telemetry": {"capture": True, "captureContent": False},
        }
        assert not plugin.remove_headers_provider()

    def test_removing_the_last_telemetry_key_drops_telemetry(self):
        plugin.set_headers_provider(PROVIDER)

        plugin.remove_headers_provider()

        assert _settings() == {}

    def test_leaves_another_headers_provider(self):
        own = {"path": "/usr/bin/my-headers", "args": []}
        _write_settings({"telemetry": {"headersProvider": own}})

        assert not plugin.remove_headers_provider()
        assert _settings()["telemetry"]["headersProvider"] == own

    def test_refuses_invalid_json_and_symlinks(self, tmp_path):
        _settings_path().parent.mkdir(parents=True)
        _settings_path().write_text("{not json")
        with pytest.raises(plugin.SettingsError):
            plugin.set_headers_provider(PROVIDER)
        assert _settings_path().read_text() == "{not json"

        target = tmp_path / "elsewhere.json"
        target.write_text("{}")
        _settings_path().unlink()
        _settings_path().symlink_to(target)
        with pytest.raises(plugin.SettingsError):
            plugin.set_headers_provider(PROVIDER)
        assert target.read_text() == "{}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_keeps_the_file_mode(self):
        _write_settings({})
        _settings_path().chmod(0o600)

        plugin.set_headers_provider(PROVIDER)

        assert _settings_path().stat().st_mode & 0o777 == 0o600


class TestTracingOnLinux:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch):
        monkeypatch.setattr(tracing, "current_os", lambda: OS.LINUX)

    def _shells(self, monkeypatch, shells):
        monkeypatch.setattr(shell_profile_env, "login_shells", lambda: shells)

    def test_enable_writes_each_login_shells_profile_and_disable_removes_exactly_that(
        self, home, monkeypatch
    ):
        self._shells(monkeypatch, ["bash", "zsh"])
        home.mkdir(parents=True)
        (home / ".zshenv").write_text("export A=1\n")
        _write_settings({"model": "m", "telemetry": {"captureContent": True}})

        assert tracing.enable(WS, PROVIDER, windows_opt_in=False)
        assert not tracing.enable(WS, PROVIDER, windows_opt_in=False)
        assert ENV["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] in (home / ".profile").read_text()
        assert shell_profile_env.BEGIN in (home / ".zshenv").read_text()
        assert _settings()["telemetry"] == {"captureContent": True, "headersProvider": PROVIDER}
        assert tracing.status() == "on"

        assert tracing.disable()
        # ug created ~/.profile only for its block; ~/.zshenv was the user's.
        assert not (home / ".profile").exists()
        assert (home / ".zshenv").read_text() == "export A=1\n"
        assert _settings() == {"model": "m", "telemetry": {"captureContent": True}}
        assert tracing.status() == "not_set_up"
        assert not tracing.disable()

    def test_never_writes_a_telemetry_key_but_headers_provider(self, monkeypatch):
        self._shells(monkeypatch, ["zsh"])

        tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert set(_settings()) == {"telemetry"}
        assert set(_settings()["telemetry"]) == {"headersProvider"}

    def test_a_shell_no_longer_used_loses_its_block(self, home, monkeypatch):
        shells = ["zsh"]
        self._shells(monkeypatch, shells)
        tracing.enable(WS, PROVIDER, windows_opt_in=False)
        shells[:] = ["fish"]

        assert tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert not (home / ".zshenv").exists()
        fish = shell_profile_env.profile_file("fish")
        assert fish is not None and fish.is_file()

    def test_a_failed_profile_write_still_leaves_revert_the_written_blocks(self, monkeypatch):
        self._shells(monkeypatch, ["bash", "zsh"])
        write = shell_profile_env.write
        calls = []

        def fail_second(path, env):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return write(path, env)

        monkeypatch.setattr(shell_profile_env, "write", fail_second)
        with pytest.raises(OSError):
            tracing.enable(WS, PROVIDER, windows_opt_in=False)
        assert shell_profile_env.BEGIN in calls[0].read_text()

        monkeypatch.setattr(shell_profile_env, "write", write)
        assert tracing.disable()
        assert not calls[0].exists()

    def test_an_unprobed_login_shell_warns_and_writes_nothing(self, home, monkeypatch, capsys):
        self._shells(monkeypatch, ["ksh"])

        assert not tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert "doesn't read the profile of your login shell (ksh)" in _out(capsys)
        assert not home.exists() or not any(home.iterdir())
        assert not _settings_path().exists()
        assert tracing.status() == "not_set_up"

    def test_switching_to_an_unprobed_shell_removes_the_earlier_setup(self, home, monkeypatch):
        shells = ["zsh"]
        self._shells(monkeypatch, shells)
        tracing.enable(WS, PROVIDER, windows_opt_in=False)
        shells[:] = ["ksh"]

        assert tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert not (home / ".zshenv").exists()
        assert _settings() == {}

    def test_the_users_own_headers_provider_keeps_tracing_off(self, home, monkeypatch, capsys):
        self._shells(monkeypatch, ["zsh"])
        own = {"path": "/usr/bin/my-headers", "args": []}
        _write_settings({"telemetry": {"headersProvider": own}})

        assert not tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert "your own telemetry.headersProvider" in _out(capsys)
        assert _settings()["telemetry"]["headersProvider"] == own
        assert not (home / ".zshenv").exists()

    def test_the_windows_opt_in_changes_nothing_here(self, monkeypatch):
        self._shells(monkeypatch, ["zsh"])
        calls = []
        monkeypatch.setattr(windows_user_env, "set_values", lambda env: calls.append(env))

        tracing.enable(WS, PROVIDER, windows_opt_in=True)

        assert calls == []


class _FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg(SimpleNamespace):
    def __init__(self, values: dict):
        super().__init__(
            HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_SZ=1, values=values
        )

    def OpenKey(self, *args):  # noqa: N802 - winreg's naming
        return _FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 1

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        del self.values[name]


@pytest.fixture
def registry(monkeypatch):
    values = {"PATH": "C:\\bin"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(values))
    broadcasts = []
    monkeypatch.setattr(windows_user_env, "_broadcast", lambda: broadcasts.append(1))
    return values, broadcasts


class TestWindowsUserEnv:
    def test_sets_only_names_the_user_hasnt_set_and_removes_only_ugs_values(self, registry):
        values, broadcasts = registry
        values["COPILOT_OTEL_ENABLED"] = "false"

        assert windows_user_env.set_values(ENV) == (True, ["COPILOT_OTEL_ENABLED"])
        assert windows_user_env.set_values(ENV) == (False, ["COPILOT_OTEL_ENABLED"])
        assert (
            values["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
            == ENV["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]
        )
        assert values["COPILOT_OTEL_ENABLED"] == "false"

        assert windows_user_env.remove_values(ENV)
        assert values == {"COPILOT_OTEL_ENABLED": "false", "PATH": "C:\\bin"}
        assert not windows_user_env.remove_values(ENV)
        assert broadcasts == [1, 1]


class TestTracingOnWindows:
    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch, registry):
        monkeypatch.setattr(tracing, "current_os", lambda: OS.WINDOWS)

    def test_off_by_default_with_a_one_line_hint(self, registry, capsys):
        values, _ = registry

        assert not tracing.enable(WS, PROVIDER, windows_opt_in=False)

        out = _out(capsys)
        assert "tracing is off on Windows" in out
        assert "--copilot-app-windows-tracing" in out
        assert values == {"PATH": "C:\\bin"}
        assert not _settings_path().exists()
        assert tracing.status() == "windows_opt_in"

    def test_opting_in_sets_the_user_environment_and_disable_removes_it(self, registry):
        values, broadcasts = registry

        assert tracing.enable(WS, PROVIDER, windows_opt_in=True)
        assert not tracing.enable(WS, PROVIDER, windows_opt_in=True)

        assert {name: values[name] for name in ENV} == ENV
        assert _settings()["telemetry"]["headersProvider"] == PROVIDER
        assert tracing.status() == "on"
        assert tracing.disable()
        assert values == {"PATH": "C:\\bin"}
        assert not _settings_path().exists() or _settings() == {}
        assert broadcasts == [1, 1]

    def test_opting_out_again_removes_what_ug_set(self, registry):
        values, _ = registry
        tracing.enable(WS, PROVIDER, windows_opt_in=True)

        assert tracing.enable(WS, PROVIDER, windows_opt_in=False)

        assert values == {"PATH": "C:\\bin"}
        assert "headersProvider" not in _settings().get("telemetry", {})

    def test_a_new_workspace_replaces_ugs_endpoint(self, registry):
        values, _ = registry
        tracing.enable(WS, PROVIDER, windows_opt_in=True)
        other = "https://other.cloud.databricks.com"

        assert tracing.enable(other, PROVIDER, windows_opt_in=True)

        assert values["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == (
            f"{other}/ai-gateway/otel/v1/traces"
        )
        tracing.disable()
        assert values == {"PATH": "C:\\bin"}

    def test_a_value_the_user_already_set_to_ugs_is_never_deleted(self, registry):
        values, _ = registry
        values["COPILOT_OTEL_ENABLED"] = "true"

        tracing.enable(WS, PROVIDER, windows_opt_in=True)
        tracing.enable(WS, PROVIDER, windows_opt_in=True)
        tracing.disable()

        assert values == {"PATH": "C:\\bin", "COPILOT_OTEL_ENABLED": "true"}

    def test_status_tells_an_opted_in_setup_that_isnt_on_from_no_opt_in(self, registry):
        values, _ = registry
        values.update(ENV)

        tracing.enable(WS, PROVIDER, windows_opt_in=True)

        assert tracing.status(windows_opt_in=True) == "not_set_up"
        assert tracing.status() == "windows_opt_in"

    def test_keeps_the_users_own_value_and_says_so(self, registry, capsys):
        values, _ = registry
        values["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = "https://collector.example.com"

        tracing.enable(WS, PROVIDER, windows_opt_in=True)

        assert "left your own OTEL_EXPORTER_OTLP_TRACES_ENDPOINT in place" in _out(capsys)
        tracing.disable()
        assert values == {
            "PATH": "C:\\bin",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://collector.example.com",
        }


def test_the_record_lives_under_ugs_app_dir(monkeypatch, home):
    monkeypatch.setattr(tracing, "current_os", lambda: OS.LINUX)
    monkeypatch.setattr(shell_profile_env, "login_shells", lambda: ["zsh"])

    tracing.enable(WS, PROVIDER, windows_opt_in=False)

    record = json.loads(tracing._record_path().read_text())
    assert record == {"profiles": [str(home / ".zshenv")], "created": [str(home / ".zshenv")]}
    assert os.fspath(tracing._record_path()).startswith(os.fspath(home.parent))


class TestAppRuntimeVersion:
    @pytest.mark.parametrize("system", [OS.LINUX, OS.MACOS, OS.WINDOWS])
    def test_the_newest_downloaded_runtime(self, monkeypatch, tmp_path, system):
        monkeypatch.setattr(tracing, "_cache_dir", _REAL_CACHE_DIR)
        monkeypatch.setattr(tracing, "current_os", lambda: system)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        cache = {
            OS.LINUX: tmp_path / ".cache",
            OS.MACOS: tmp_path / "Library" / "Caches",
            OS.WINDOWS: tmp_path / "local",
        }[system]
        cli = cache / "github-copilot-sdk" / "cli"
        for name in ("copilot-sdk-runtime-1.0.98", "copilot-sdk-runtime-1.0.106-8", "notes"):
            (cli / name).mkdir(parents=True)

        assert tracing.app_runtime_version() == (1, 0, 106)

    def test_none_before_the_app_downloaded_one(self):
        assert tracing.app_runtime_version() is None


def test_disable_leaves_no_empty_record_directory(monkeypatch):
    monkeypatch.setattr(tracing, "current_os", lambda: OS.LINUX)
    monkeypatch.setattr(shell_profile_env, "login_shells", lambda: ["zsh"])
    tracing.enable(WS, PROVIDER, windows_opt_in=False)

    tracing.disable()

    assert not tracing._record_path().parent.exists()
