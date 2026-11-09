from __future__ import annotations

import base64
import json
import os
import runpy
import signal
import stat
import subprocess
import sys
import time

import pytest

from ucode import entrypoint, token_cache

# conftest replaces _spawn_detached with a no-op; keep the real one for the tests that need it.
_REAL_SPAWN_DETACHED = token_cache._spawn_detached


def _jwt(expires_at: float, sub: str = "user") -> str:
    claims = json.dumps({"exp": expires_at, "sub": sub}).encode()
    return f"h.{base64.urlsafe_b64encode(claims).decode().rstrip('=')}.s"


def _age(host: str, profile: str | None, seconds: float) -> None:
    """Backdate a cache entry's ``cached_at`` by ``seconds``."""
    path = token_cache._cache_path(host, profile)
    entry = json.loads(path.read_text())
    entry["cached_at"] -= seconds
    path.write_text(json.dumps(entry))


@pytest.fixture(autouse=True)
def _no_bearer_env(monkeypatch):
    monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
    monkeypatch.delenv("DATABRICKS_BEARER_COMMAND", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)


@pytest.fixture(autouse=True)
def _restore_sigterm():
    # entrypoint.main() installs the auth-token kill logger in this process.
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.fixture
def spawned(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(token_cache, "_spawn_detached", calls.append)
    return calls


WS = "https://example.cloud.databricks.com"


class TestReadWrite:
    def test_round_trip(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "prod", token)
        assert token_cache.read(WS, "prod") == token

    def test_key_normalizes_host_but_separates_profiles(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS + "/", "prod", token)
        assert token_cache.read(WS.upper(), "prod") == token
        assert token_cache.read(WS, None) is None
        assert token_cache.read(WS, "other") is None

    def test_config_file_is_part_of_the_key(self, monkeypatch):
        token_cache.write(WS, "prod", _jwt(time.time() + 3600))
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "/elsewhere/.databrickscfg")
        assert token_cache.read(WS, "prod") is None

    def test_near_expiry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + token_cache.MIN_REMAINING_SECONDS - 1))
        assert token_cache.read(WS, None) is None

    def test_fresh_entry_is_not_due_for_refresh(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        assert token_cache.lookup(WS, None) == (token, False)

    def test_old_entry_is_still_served_but_due_for_refresh(self):
        # Age alone never makes a miss: the JWT's expiry decides whether to serve.
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, None, token)
        later = time.time() + token_cache.REFRESH_AFTER_SECONDS + 1
        assert token_cache.lookup(WS, None, now=later) == (token, True)
        assert token_cache.read(WS, None, now=later) == token

    def test_old_entry_near_expiry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        later = time.time() + 3600 - token_cache.MIN_REMAINING_SECONDS + 1
        assert token_cache.lookup(WS, None, now=later) is None

    def test_entry_stamped_in_the_future_is_due_for_refresh(self):
        # The clock moved back after the write; re-check instead of trusting the stamp.
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        assert token_cache.lookup(WS, None, now=time.time() - 60) == (token, True)

    def test_non_jwt_is_never_cached(self):
        token_cache.write(WS, None, "dapi-opaque-pat")
        assert not token_cache.CACHE_DIR.exists()

    @pytest.mark.parametrize("env", ["DATABRICKS_BEARER", "DATABRICKS_BEARER_COMMAND"])
    def test_env_overrides_disable_cache(self, monkeypatch, env):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        monkeypatch.setenv(env, "x")
        assert token_cache.read(WS, None) is None
        token_cache.write(WS, "other", token)
        monkeypatch.delenv(env)
        assert token_cache.read(WS, "other") is None

    def test_corrupt_entry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        (entry,) = token_cache.CACHE_DIR.glob("*.json")
        entry.write_text("{not json")
        assert token_cache.read(WS, None) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_files_are_private(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        assert stat.S_IMODE(token_cache.CACHE_DIR.stat().st_mode) == 0o700
        (entry,) = token_cache.CACHE_DIR.glob("*.json")
        assert stat.S_IMODE(entry.stat().st_mode) == 0o600

    def test_clear(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        token_cache.clear()
        assert token_cache.read(WS, None) is None

    def test_clear_also_drops_refresh_markers(self, spawned):
        token_cache.write(WS, None, _jwt(time.time() + 86400))
        _age(WS, None, token_cache.REFRESH_AFTER_SECONDS + 1)
        token_cache.serve(WS, None)
        assert token_cache._refresh_marker_path(WS, None).exists()
        token_cache.clear()
        assert not token_cache._refresh_marker_path(WS, None).exists()

    def test_fetch_started_before_a_clear_is_not_written_back(self):
        # A helper or refresher still fetching when a login clears the cache holds the old
        # identity's token; it must not reappear for the next helper.
        token_cache.write(WS, "other", _jwt(time.time() + 3600))
        started = time.time()
        token_cache.clear()
        token_cache.write(WS, "p", _jwt(time.time() + 3600), fetch_started=started)
        assert token_cache.read(WS, "p") is None
        assert list(token_cache.CACHE_DIR.glob("*.tmp")) == []

    def test_clear_without_a_cache_creates_nothing(self):
        token_cache.clear()
        assert not token_cache.CACHE_DIR.exists()

    def test_fetch_started_after_a_clear_is_written(self):
        token_cache.clear()
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token, fetch_started=time.time() + 0.001)
        assert token_cache.read(WS, "p") == token

    def test_relative_config_file_is_keyed_by_the_file_it_names(self, monkeypatch, tmp_path):
        # The CLI resolves a relative DATABRICKS_CONFIG_FILE against the working directory.
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", ".databrickscfg")
        monkeypatch.chdir(first)
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        assert token_cache.read(WS, "p") == token
        monkeypatch.chdir(second)
        assert token_cache.read(WS, "p") is None
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(first / ".databrickscfg"))
        assert token_cache.read(WS, "p") == token


class TestBackgroundRefresh:
    def test_old_entry_is_served_and_starts_a_refresh(self, spawned):
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        assert token_cache.serve(WS, "prod") == token
        assert spawned == [token_cache.refresh_command(WS, "prod")]

    def test_fresh_entry_starts_nothing(self, spawned):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "prod", token)
        assert token_cache.serve(WS, "prod") == token
        assert spawned == []

    def test_miss_starts_nothing(self, spawned):
        # A miss is the caller's to fetch in the foreground; nothing to re-check.
        assert token_cache.serve(WS, "prod") is None
        assert spawned == []

    def test_refresh_is_throttled_per_entry(self, spawned):
        token = _jwt(time.time() + 86400)
        for profile in ("prod", "dev"):
            token_cache.write(WS, profile, token)
            _age(WS, profile, token_cache.REFRESH_AFTER_SECONDS + 1)
        for _ in range(3):
            assert token_cache.serve(WS, "prod") == token
        token_cache.serve(WS, "dev")
        assert spawned == [
            token_cache.refresh_command(WS, "prod"),
            token_cache.refresh_command(WS, "dev"),
        ]

    def test_refresh_is_retried_after_the_window(self, spawned):
        # A refresher that failed leaves the entry old; the next window retries it.
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        token_cache.serve(WS, "prod")
        marker = token_cache._refresh_marker_path(WS, "prod")
        past = time.time() - token_cache.REFRESH_RETRY_SECONDS - 1
        os.utime(marker, (past, past))
        token_cache.serve(WS, "prod")
        assert len(spawned) == 2

    def test_spawn_failure_still_serves_the_token(self, monkeypatch):
        def boom(argv):
            raise OSError("no fork for you")

        monkeypatch.setattr(token_cache, "_spawn_detached", boom)
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, None, token)
        _age(WS, None, token_cache.REFRESH_AFTER_SECONDS + 1)
        assert token_cache.serve(WS, None) == token

    def test_refresh_command(self):
        assert token_cache.refresh_command(WS, "prod") == [
            sys.executable,
            "-P",
            "-m",
            "ucode",
            "auth-token",
            "--host",
            WS,
            "--profile",
            "prod",
            "--refresh-cache",
        ]
        assert "--profile" not in token_cache.refresh_command(WS, None)

    def test_refresher_inherits_no_stdio(self, monkeypatch):
        # pi's execSync waits for every holder of the helper's stdout pipe, so the
        # refresher must not inherit it, and must outlive the helper's process group.
        seen: dict[str, object] = {}

        def fake_popen(argv, **kwargs):
            seen.update(kwargs, argv=argv)

        monkeypatch.setattr("ucode.os_compatibility.subprocess_cross_os.popen", fake_popen)
        _REAL_SPAWN_DETACHED(["refresher"])
        assert seen["argv"] == ["refresher"]
        for stream in ("stdin", "stdout", "stderr"):
            assert seen[stream] == subprocess.DEVNULL
        assert seen["close_fds"] is True
        if sys.platform == "win32":
            assert seen["creationflags"]
        else:
            assert seen["start_new_session"] is True

    def test_python_dash_m_ucode_runs_the_entry_point(self, monkeypatch):
        calls = []
        monkeypatch.setattr("ucode.entrypoint.main", lambda: calls.append(True))
        runpy.run_module("ucode", run_name="__main__")
        assert calls == [True]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell-script fake CLI")
    def test_refresher_rewrites_the_cache_end_to_end(self, tmp_path):
        """The real refresher argv, against a fake CLI, in a throwaway HOME."""
        home, bin_dir = tmp_path / "home", tmp_path / "bin"
        home.mkdir()
        bin_dir.mkdir()
        token = _jwt(time.time() + 3600, "fresh")
        fake_cli = bin_dir / "databricks"
        fake_cli.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = --version ]; then echo "Databricks CLI v999.0.0"; exit 0; fi\n'
            f"echo '{json.dumps({'access_token': token})}'\n"
        )
        fake_cli.chmod(0o755)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DATABRICKS_", "UCODE_"))
        }
        env.update(HOME=str(home), PATH=f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
        result = subprocess.run(
            token_cache.refresh_command(WS, "prod"),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        (entry,) = (home / ".ucode" / "token-cache").glob("*.json")
        assert json.loads(entry.read_text())["token"] == token


class TestFastPath:
    @pytest.fixture
    def cached(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        token_cache.write(WS, "prod", token)
        return token

    def test_host_only(self, cached):
        assert token_cache.fast_path_token(["auth-token", "--host", WS]) == cached

    def test_pi_caller_timeout_is_served(self, cached):
        argv = ["auth-token", "--host", WS, "--profile", "prod", "--caller-timeout", "10"]
        assert token_cache.fast_path_token(argv) == cached

    def test_host_and_profile_in_either_order(self, cached):
        argv = ["auth-token", "--profile", "prod", "--host", WS]
        assert token_cache.fast_path_token(argv) == cached

    def test_old_entry_is_served_and_refreshed_in_background(self, cached, spawned):
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        argv = ["auth-token", "--host", WS, "--profile", "prod"]
        assert token_cache.fast_path_token(argv) == cached
        assert spawned == [token_cache.refresh_command(WS, "prod")]

    @pytest.mark.parametrize(
        "argv",
        [
            ["auth-token"],
            ["auth-token", "--profile", "prod"],
            ["auth-token", "--host", WS, "--use-pat"],
            ["auth-token", "--host", WS, "--force-refresh"],
            ["auth-token", "--host", WS, "--browser-login"],
            ["auth-token", "--host", WS, "--refresh-cache"],
            ["auth-token", "--host", WS, "--profile", "prod", "--refresh-cache"],
            ["auth-token", "--host", WS, "--client-id", "c"],
            ["auth-token", "--host", WS, "--host", WS],
            ["auth-token", "--caller-timeout", "10"],
            ["auth-token", "--host", WS, "--caller-timeout", "10", "--use-pat"],
            ["auth-token", f"--host={WS}"],
            ["configure", "--host", WS],
        ],
    )
    def test_other_shapes_defer_to_full_cli(self, cached, argv):
        assert token_cache.fast_path_token(argv) is None


class TestSavedPatMode:
    """The full command honors a saved workspace's PAT mode for any host, so the fast path must."""

    def _save_state(self, use_pat: bool) -> None:
        from ucode.state_file import STATE_VERSION

        token_cache.STATE_PATH.write_text(
            json.dumps(
                {
                    "state_version": STATE_VERSION,
                    "current_workspace": WS,
                    "workspaces": {WS: {"profile": "p", "use_pat": use_pat}},
                }
            )
        )

    def test_saved_pat_mode_defers_to_the_full_cli(self):
        token_cache.write(WS, "p", _jwt(time.time() + 3600))
        self._save_state(use_pat=True)
        assert token_cache.fast_path_token(["auth-token", "--host", WS, "--profile", "p"]) is None

    def test_saved_oauth_mode_is_served(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        self._save_state(use_pat=False)
        assert token_cache.fast_path_token(["auth-token", "--host", WS, "--profile", "p"]) == token

    def test_missing_or_corrupt_state_is_served(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        argv = ["auth-token", "--host", WS, "--profile", "p"]
        assert token_cache.fast_path_token(argv) == token
        token_cache.STATE_PATH.write_text("{not json")
        assert token_cache.fast_path_token(argv) == token


class TestEntry:
    def test_cache_hit_skips_full_cli(self, monkeypatch, capsys):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS])
        monkeypatch.setattr("ucode.cli.main", lambda: pytest.fail("full CLI invoked"))
        entrypoint.main()
        assert capsys.readouterr().out == token + "\n"

    def test_old_entry_is_served_without_the_full_cli(self, monkeypatch, capsys, spawned):
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, None, token)
        _age(WS, None, token_cache.REFRESH_AFTER_SECONDS + 1)
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS])
        monkeypatch.setattr("ucode.cli.main", lambda: pytest.fail("full CLI invoked"))
        entrypoint.main()
        assert capsys.readouterr().out == token + "\n"
        assert spawned == [token_cache.refresh_command(WS, None)]

    def test_cache_miss_delegates_to_full_cli(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS])
        monkeypatch.setattr("ucode.cli.main", lambda: calls.append(True))
        entrypoint.main()
        assert calls == [True]
