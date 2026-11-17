from __future__ import annotations

import base64
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ucode import entrypoint, token_cache

# conftest replaces _spawn_detached with a no-op; keep the real one for the tests that need it.
_REAL_SPAWN_DETACHED = token_cache._spawn_detached


def _deny_entry_deletes(monkeypatch) -> None:
    """Make deleting a cache entry fail, as a sharing violation does on Windows."""
    path_type = type(token_cache.CACHE_DIR)
    real_unlink = path_type.unlink

    def unlink(path, *args, **kwargs):
        if path.suffix == ".json":
            raise PermissionError(13, "sharing violation", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "unlink", unlink)


def _served(host: str, profile: str | None, **kwargs) -> str | None:
    """The token ``token_cache.lookup`` would serve, or None."""
    hit = token_cache.lookup(host, profile, **kwargs)
    return hit[0] if hit else None


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
    for name in token_cache._IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


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
        assert _served(WS, "prod") == token

    def test_key_normalizes_host_but_separates_profiles(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS + "/", "prod", token)
        assert _served(WS.upper(), "prod") == token
        assert _served(WS, None) is None
        assert _served(WS, "other") is None

    def test_key_adds_a_missing_scheme(self):
        token = _jwt(time.time() + 3600)
        token_cache.write("Example.cloud.databricks.com/", "prod", token)
        assert _served(WS, "prod") == token

    def test_config_file_is_part_of_the_key(self, monkeypatch):
        token_cache.write(WS, "prod", _jwt(time.time() + 3600))
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "/elsewhere/.databrickscfg")
        assert _served(WS, "prod") is None

    def test_xdg_config_home_is_part_of_the_key(self, monkeypatch, tmp_path):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "prod", token)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert _served(WS, "prod") is None
        monkeypatch.delenv("XDG_CONFIG_HOME")
        assert _served(WS, "prod") == token

    @pytest.mark.parametrize("explicit", [False, True])
    def test_editing_the_config_file_drops_its_entries(self, monkeypatch, tmp_path, explicit):
        # A profile repointed at another host or identity in place must not keep its old token.
        if explicit:
            config = tmp_path / "custom.cfg"
            monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
        else:
            monkeypatch.setenv("HOME", str(tmp_path))
            monkeypatch.setenv("USERPROFILE", str(tmp_path))
            config = tmp_path / ".databrickscfg"
        config.write_text("[prod]\nhost = https://a\n")
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "prod", token)
        assert _served(WS, "prod") == token
        config.write_text("[prod]\nhost = https://ab\n")
        assert _served(WS, "prod") is None
        token_cache.write(WS, "prod", token)
        assert _served(WS, "prod") == token
        # Same size, later mtime.
        config.write_text("[prod]\nhost = https://ac\n")
        later = config.stat().st_mtime + 10
        os.utime(config, (later, later))
        assert _served(WS, "prod") is None
        # Edited while a fetch was in flight: its token may be the old config's identity.
        config_before = token_cache.config_fingerprint()
        config.write_text("[prod]\nhost = https://abcd\n")
        token_cache.write(WS, "prod", token, expected_config=config_before)
        assert token_cache.lookup(WS, "prod") is None
        token_cache.write(WS, "prod", token, expected_config=token_cache.config_fingerprint())
        assert _served(WS, "prod") == token

    def test_near_expiry_is_a_miss(self):
        token_cache.write(WS, None, _jwt(time.time() + token_cache.MIN_REMAINING_SECONDS - 1))
        assert _served(WS, None) is None

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
        assert _served(WS, None, now=later) == token

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

    @pytest.mark.parametrize("env", token_cache._IDENTITY_ENV_VARS)
    def test_env_overrides_disable_cache(self, monkeypatch, env):
        # An entry cached without the variable is not served with it, and nothing is cached
        # while it is set.
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        monkeypatch.setenv(env, "x")
        assert _served(WS, None) is None
        token_cache.write(WS, "other", token)
        monkeypatch.delenv(env)
        assert _served(WS, "other") is None

    @pytest.mark.parametrize(
        ("content", "served"),
        [
            ("{not json", False),
            ('{"token": 1, "cached_at": 0}', False),
            ('{"token": "TOKEN"}', False),
            # A stamp in the far future is served, but due for a re-check at once.
            ('{"token": "TOKEN", "cached_at": 1e999}', True),
        ],
    )
    def test_corrupt_entry_is_a_miss(self, content, served):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        (entry,) = token_cache.CACHE_DIR.glob("*.json")
        entry.write_text(content.replace("TOKEN", token))
        assert token_cache.lookup(WS, None) == ((token, True) if served else None)

    def test_oversized_numbers_are_a_miss(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        (entry,) = token_cache.CACHE_DIR.glob("*.json")
        entry.write_text(f'{{"token": "{token}", "cached_at": 1{"0" * 400}}}')
        assert token_cache.lookup(WS, None) is None
        huge_exp = base64.urlsafe_b64encode(f'{{"exp": 1{"0" * 400}}}'.encode()).decode()
        assert token_cache.token_expiry(f"h.{huge_exp.rstrip('=')}.s") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_files_are_private(self, spawned):
        token_cache.write(WS, None, _jwt(time.time() + 86400))
        _age(WS, None, token_cache.REFRESH_AFTER_SECONDS + 1)
        token_cache.serve(WS, None)
        marker = token_cache._refresh_marker_path(WS, None)
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        token_cache.clear()
        token_cache.write(WS, None, _jwt(time.time() + 86400))
        assert stat.S_IMODE(token_cache.CACHE_DIR.stat().st_mode) == 0o700
        names = {path.name for path in token_cache.CACHE_DIR.iterdir()}
        assert {token_cache._LOCK_NAME, token_cache._CLEARED_NAME} <= names
        for path in token_cache.CACHE_DIR.iterdir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_loose_permissions_are_tightened(self):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        token_cache.clear()
        token_cache.CACHE_DIR.chmod(0o755)
        for name in (token_cache._LOCK_NAME, token_cache._CLEARED_NAME):
            (token_cache.CACHE_DIR / name).chmod(0o644)
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        assert _served(WS, None) == token
        token_cache.clear()
        assert stat.S_IMODE(token_cache.CACHE_DIR.stat().st_mode) == 0o700
        for name in (token_cache._LOCK_NAME, token_cache._CLEARED_NAME):
            assert stat.S_IMODE((token_cache.CACHE_DIR / name).stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_linked_cache_dir_is_refused(self, tmp_path):
        target = tmp_path / "elsewhere"
        target.mkdir(mode=0o700)
        token_cache.CACHE_DIR.symlink_to(target)
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        token_cache.clear()
        assert list(target.iterdir()) == []
        # An entry planted through the link is never served either.
        (target / token_cache._cache_path(WS, None).name).write_text(
            json.dumps({"token": token, "cached_at": time.time()})
        )
        assert _served(WS, None) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_linked_entry_and_clear_stamp_are_refused(self, tmp_path):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        planted = tmp_path / "planted.json"
        planted.write_text(json.dumps({"token": token, "cached_at": time.time() + 1}))
        entry = token_cache._cache_path(WS, None)
        entry.unlink()
        entry.symlink_to(planted)
        assert _served(WS, None) is None
        # A clear replaces a linked stamp rather than writing through it.
        victim = tmp_path / "victim"
        victim.write_text("untouched")
        (token_cache.CACHE_DIR / token_cache._CLEARED_NAME).symlink_to(victim)
        token_cache.clear()
        assert victim.read_text() == "untouched"
        assert not (token_cache.CACHE_DIR / token_cache._CLEARED_NAME).is_symlink()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_clear_still_clears_when_the_lock_cannot_be_opened(self, tmp_path):
        # A broken or foreign `.lock` must not keep a login from invalidating the cache.
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        lock = token_cache.CACHE_DIR / token_cache._LOCK_NAME
        lock.unlink()
        lock.symlink_to(tmp_path / "missing-target")
        token_cache.clear()
        assert list(token_cache.CACHE_DIR.glob("*.json")) == []
        assert token_cache.lookup(WS, None) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
    def test_cache_dir_owned_by_another_user_is_refused(self, monkeypatch):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        other_uid = os.getuid() + 1
        monkeypatch.setattr(token_cache.os, "getuid", lambda: other_uid)
        assert _served(WS, None) is None
        token_cache.write(WS, "p", token)
        assert list(token_cache.CACHE_DIR.glob("*.json")) == [token_cache._cache_path(WS, None)]

    @pytest.mark.parametrize("delete_fails", [False, True])
    def test_clear(self, monkeypatch, delete_fails):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        (entry,) = token_cache.CACHE_DIR.glob("*.json")
        if delete_fails:
            # On Windows a concurrent reader can block the delete (a sharing violation).
            _deny_entry_deletes(monkeypatch)
        token_cache.clear()
        assert entry.exists() == delete_fails
        # The surviving entry predates the clear, so it is never served again.
        assert token_cache.lookup(WS, None) is None

    def test_clears_and_writes_stay_ordered_within_one_clock_tick(self, monkeypatch):
        # time.time() can return the same value for consecutive calls (about 16ms steps on
        # Windows with Python 3.12); stamps must still order a clear against each write.
        frozen = time.time()
        monkeypatch.setattr(
            token_cache, "time", SimpleNamespace(**{**vars(time), "time": lambda: frozen})
        )
        _deny_entry_deletes(monkeypatch)  # so only the stamps decide
        token = _jwt(frozen + 3600)
        token_cache.write(WS, None, token)
        token_cache.clear()
        assert token_cache.lookup(WS, None) is None
        token_cache.write(WS, None, token)
        assert token_cache.lookup(WS, None) == (token, False)
        token_cache.clear()
        assert token_cache.lookup(WS, None) is None

    def test_entry_written_after_a_clear_is_served(self):
        token_cache.clear()
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        assert token_cache.lookup(WS, None) == (token, False)

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
        assert _served(WS, "p") is None
        assert list(token_cache.CACHE_DIR.glob("*.tmp")) == []

    @pytest.mark.parametrize("entry_delete_fails", [False, True])
    def test_clear_overtaking_a_paused_locked_write_wins(self, monkeypatch, entry_delete_fails):
        # A writer holding the lock stalls after reading the clear stamp; a login's clear times
        # out on the lock, clears without it, and the writer then resumes.
        token_cache.clear()
        monkeypatch.setattr(token_cache, "_LOCK_TIMEOUT_SECONDS", 0.05)
        if entry_delete_fails:
            _deny_entry_deletes(monkeypatch)
        paused, resume = threading.Event(), threading.Event()
        real_cleared_at = token_cache._cleared_at
        writer_reads: list[float] = []

        def cleared_at() -> float:
            value = real_cleared_at()
            if threading.current_thread().name == "writer":
                writer_reads.append(value)
                if len(writer_reads) == 1:
                    paused.set()
                    assert resume.wait(timeout=30)
            return value

        monkeypatch.setattr(token_cache, "_cleared_at", cleared_at)
        started = time.time()
        writer = threading.Thread(
            target=token_cache.write,
            args=(WS, "p", _jwt(time.time() + 3600)),
            kwargs={"fetch_started": started},
            name="writer",
        )
        writer.start()
        try:
            assert paused.wait(timeout=30)
            token_cache.clear()  # times out on the writer's lock and clears unlocked
        finally:
            resume.set()
            writer.join(timeout=30)
        assert not writer.is_alive()
        assert len(writer_reads) == 2  # the writer re-checked the stamp after its replace
        assert token_cache.lookup(WS, "p") is None
        assert token_cache._cache_path(WS, "p").exists() == entry_delete_fails

    def test_first_fetch_racing_a_login_is_not_written_back(self):
        # No helper has created the cache yet: a first fetch for the old identity is in flight
        # when a login clears the cache, then tries to write.
        assert not token_cache.CACHE_DIR.exists()
        started = time.time()
        token_cache.clear()
        token_cache.write(WS, "p", _jwt(time.time() + 3600), fetch_started=started)
        assert _served(WS, "p") is None
        assert list(token_cache.CACHE_DIR.glob("*.json")) == []

    def test_fetch_started_after_a_clear_is_written(self):
        token_cache.clear()
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token, fetch_started=time.time() + 0.001)
        assert _served(WS, "p") == token

    def test_relative_config_file_is_keyed_by_the_file_it_names(self, monkeypatch, tmp_path):
        # The CLI resolves a relative DATABRICKS_CONFIG_FILE against the working directory.
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", ".databrickscfg")
        monkeypatch.chdir(first)
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        assert _served(WS, "p") == token
        monkeypatch.chdir(second)
        assert _served(WS, "p") is None
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(first / ".databrickscfg"))
        assert _served(WS, "p") == token


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

    def test_throttle_holds_when_file_times_run_ahead_of_time_time(self, monkeypatch, spawned):
        # On Windows, Python 3.12's time.time() reads the coarse system clock (about 16ms
        # steps) while NTFS stamps files from a finer one, so a marker created just now can
        # carry an mtime later than time.time(). Measuring the claim's age from its mtime made
        # it look stamped in the future, so every request re-claimed it and spawned a refresher.
        # A lagging time.time() reproduces that on any OS.
        lagging = SimpleNamespace(**{**vars(time), "time": lambda: time.time() - 0.5})
        monkeypatch.setattr(token_cache, "time", lagging)
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        for _ in range(3):
            assert token_cache.serve(WS, "prod") == token
        assert spawned == [token_cache.refresh_command(WS, "prod")]

    def test_refresh_is_retried_after_the_window(self, spawned):
        # A refresher that failed leaves the entry old; the next window retries it.
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        token_cache.serve(WS, "prod")
        marker = token_cache._refresh_marker_path(WS, "prod")
        marker.write_text(repr(time.time() - token_cache.REFRESH_RETRY_SECONDS - 1))
        token_cache.serve(WS, "prod")
        assert len(spawned) == 2

    def test_claim_stamped_in_the_future_is_retried(self, spawned):
        # The clock moved back after the claim; don't wait out a window that may never end.
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        token_cache.serve(WS, "prod")
        marker = token_cache._refresh_marker_path(WS, "prod")
        marker.write_text(repr(time.time() + token_cache.REFRESH_RETRY_SECONDS))
        token_cache.serve(WS, "prod")
        assert len(spawned) == 2

    @pytest.mark.parametrize("content", ["", "not a time"])
    def test_unreadable_claim_falls_back_to_its_file_time(self, spawned, content):
        # A claim whose stamp never got written (or an older ug's empty marker).
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        marker = token_cache._refresh_marker_path(WS, "prod")
        marker.write_text(content)
        token_cache.serve(WS, "prod")
        assert spawned == []
        past = time.time() - token_cache.REFRESH_RETRY_SECONDS - 1
        os.utime(marker, (past, past))
        token_cache.serve(WS, "prod")
        assert len(spawned) == 1

    def test_claim_file_time_just_ahead_of_now_is_still_active(self):
        # NTFS stamps the marker from a finer clock than time.time(), so a claim taken just now
        # can read a few milliseconds in the future; it must still hold the window.
        token_cache._ensure_private_cache_dir(create=True)
        marker = token_cache._refresh_marker_path(WS, "prod")
        marker.write_text("")
        now = time.time()
        ahead = now + 0.02
        assert ahead - now < token_cache._FUTURE_STAMP_TOLERANCE_SECONDS
        os.utime(marker, (ahead, ahead))
        assert token_cache._claim_refresh(marker, now) is False
        assert marker.exists()

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


class TestSavedChoices:
    """With ``--host`` the full command applies saved choices only on a machine with a published
    agent configuration, so the fast path defers only then."""

    ARGV = ["auth-token", "--host", WS, "--profile", "p"]
    ARGV_NO_PROFILE = ["auth-token", "--host", WS]

    def _save_state(self, **choices) -> None:
        from ucode.state_file import STATE_VERSION

        token_cache.STATE_PATH.write_text(
            json.dumps(
                {
                    "state_version": STATE_VERSION,
                    "current_workspace": WS,
                    "workspaces": {WS: choices},
                }
            )
        )

    @pytest.fixture
    def published(self, monkeypatch):
        monkeypatch.setattr(token_cache, "_published_agent_config_present", lambda: True)

    def test_saved_pat_is_ignored_without_a_published_config(self):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        self._save_state(profile="p", use_pat=True)
        assert token_cache.fast_path_token(self.ARGV) == token

    def test_saved_pat_defers_when_published(self, published):
        token_cache.write(WS, "p", _jwt(time.time() + 3600))
        self._save_state(profile="p", use_pat=True)
        assert (
            token_cache.fast_path_token(["auth-token", "--host", WS + "/", "--profile", "p"])
            is None
        )

    def test_unrelated_host_keeps_fast_path_when_published(self, published):
        other = "https://other.cloud.databricks.com"
        token = _jwt(time.time() + 3600)
        token_cache.write(other, "p", token)
        token_cache.write(other, None, token)
        self._save_state(profile="q", use_pat=True)
        assert (
            token_cache.fast_path_token(["auth-token", "--host", other, "--profile", "p"]) == token
        )
        assert token_cache.fast_path_token(["auth-token", "--host", other]) == token

    def test_saved_oauth_is_served_when_published(self, published):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        self._save_state(profile="p", use_pat=False)
        assert token_cache.fast_path_token(self.ARGV) == token

    @pytest.mark.parametrize(
        "choice",
        [{"profile": "q"}, {"custom_oauth": {"client_id": "c", "scopes": ["all-apis"]}}],
    )
    def test_no_profile_defers_to_saved_profile_or_oauth(self, published, choice):
        token_cache.write(WS, None, _jwt(time.time() + 3600))
        self._save_state(**choice)
        assert token_cache.fast_path_token(self.ARGV_NO_PROFILE) is None

    def test_no_profile_without_saved_choices_is_served(self, published):
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, None, token)
        self._save_state(use_pat=False)
        assert token_cache.fast_path_token(self.ARGV_NO_PROFILE) == token
        token_cache.STATE_PATH.write_text("{not json")
        assert token_cache.fast_path_token(self.ARGV_NO_PROFILE) == token
        token_cache.STATE_PATH.unlink()
        assert token_cache.fast_path_token(self.ARGV_NO_PROFILE) == token

    def test_marker_checks_owner_mode_and_json_dict(self, tmp_path, monkeypatch):
        import stat as stat_mod

        marker = tmp_path / "marker"
        monkeypatch.setattr(token_cache, "PUBLISHED_AGENT_CONFIG_MARKER_PATH", marker)
        monkeypatch.setattr(
            token_cache, "sys", SimpleNamespace(**{**vars(sys), "platform": "linux"})
        )
        assert token_cache._published_agent_config_present() is False
        marker.write_text("{}")

        real_lstat = type(marker).lstat

        def patch_stat(uid: int, mode: int) -> None:
            monkeypatch.setattr(
                type(marker),
                "lstat",
                lambda self: (
                    SimpleNamespace(st_uid=uid, st_mode=stat_mod.S_IFREG | mode)
                    if self == marker
                    else real_lstat(self)
                ),
            )

        patch_stat(1000, 0o644)
        assert token_cache._published_agent_config_present() is False
        patch_stat(0, 0o664)
        assert token_cache._published_agent_config_present() is False
        patch_stat(0, 0o644)
        assert token_cache._published_agent_config_present() is True
        marker.write_text("[]")
        assert token_cache._published_agent_config_present() is False
        marker.write_text("{bad")
        assert token_cache._published_agent_config_present() is False


class TestEntry:
    @pytest.mark.parametrize("old", [False, True])
    def test_cache_hit_skips_full_cli(self, monkeypatch, capsys, spawned, old):
        token = _jwt(time.time() + 86400)
        token_cache.write(WS, "prod", token)
        if old:
            _age(WS, "prod", token_cache.REFRESH_AFTER_SECONDS + 1)
        monkeypatch.setattr(sys, "argv", ["ug", "auth-token", "--host", WS, "--profile", "prod"])
        monkeypatch.setattr("ucode.cli.main", lambda: pytest.fail("full CLI invoked"))
        entrypoint.main()
        assert capsys.readouterr().out == token + "\n"
        # Only an old entry starts the background refresher, for the same profile.
        assert spawned == ([token_cache.refresh_command(WS, "prod")] if old else [])


class TestWindowsAcl:
    """On Windows the cache is used only once icacls has restricted its directory."""

    SID = "S-1-5-21-1-2-3-1001"

    def _patch(self, monkeypatch, sid, *, returncode=0, error=None, windows=True):
        from pathlib import PureWindowsPath

        from ucode.os_compatibility import subprocess_cross_os, system_cross_os

        calls: list[list[str]] = []

        def run(argv, **kw):
            calls.append(argv)
            if error is not None:
                raise error
            return SimpleNamespace(returncode=returncode)

        monkeypatch.setattr(subprocess_cross_os, "run", run)
        monkeypatch.setattr(
            system_cross_os, "windows_system32_dir", lambda: PureWindowsPath("C:/Windows/System32")
        )
        monkeypatch.setattr(token_cache, "_current_user_sid", lambda: sid)
        if windows:
            monkeypatch.setattr(
                token_cache, "sys", SimpleNamespace(**{**vars(sys), "platform": "win32"})
            )
        return calls

    def _marker(self):
        return token_cache.CACHE_DIR / token_cache._ACL_MARKER_NAME

    def test_resolved_sid_is_granted_without_following_links(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch, self.SID, windows=False)
        assert token_cache._restrict_windows_dir(tmp_path) is True
        assert len(calls) == 1
        assert "/L" in calls[0]
        assert "/inheritance:r" in calls[0]
        assert f"*{self.SID}:(OI)(CI)F" in calls[0]

    @pytest.mark.parametrize("sid", ["", "garbage"])
    def test_unresolved_sid_skips_icacls(self, monkeypatch, tmp_path, sid):
        calls = self._patch(monkeypatch, sid, windows=False)
        assert token_cache._restrict_windows_dir(tmp_path) is False
        assert calls == []

    def test_fresh_directory_is_restricted_then_used(self, monkeypatch):
        calls = self._patch(monkeypatch, self.SID)
        token = _jwt(time.time() + 3600)
        token_cache.write(WS, "p", token)
        assert len(calls) == 1
        assert self._marker().is_file()
        assert _served(WS, "p") == token
        # Once marked, the directory is not restricted again.
        token_cache.write(WS, "q", token)
        assert len(calls) == 1

    @pytest.mark.parametrize("icacls_ok", [True, False])
    def test_existing_unmarked_directory_is_restricted_before_use(self, monkeypatch, icacls_ok):
        # A directory an earlier ug created (or anyone planted) without the restriction.
        token = _jwt(time.time() + 3600)
        token_cache.CACHE_DIR.mkdir()
        token_cache._cache_path(WS, "p").write_text(
            json.dumps({"token": token, "cached_at": time.time()})
        )
        calls = self._patch(monkeypatch, self.SID, returncode=0 if icacls_ok else 5)
        assert token_cache.lookup(WS, "p") is None
        assert calls == []  # a read never restricts
        token_cache.write(WS, "q", token)
        assert len(calls) == 1
        assert self._marker().exists() == icacls_ok
        assert token_cache._cache_path(WS, "q").exists() == icacls_ok
        assert _served(WS, "q") == (token if icacls_ok else None)

    @pytest.mark.parametrize("sid", ["", "garbage"])
    def test_unresolved_sid_disables_the_cache(self, monkeypatch, sid):
        calls = self._patch(monkeypatch, sid)
        token_cache.write(WS, "p", _jwt(time.time() + 3600))
        assert calls == []
        assert list(token_cache.CACHE_DIR.glob("*.json")) == []
        assert not self._marker().exists()
        assert token_cache.lookup(WS, "p") is None

    @pytest.mark.parametrize(
        ("returncode", "error"), [(1, None), (0, OSError("no icacls"))], ids=["exit", "oserror"]
    )
    def test_failed_icacls_disables_the_cache(self, monkeypatch, returncode, error):
        calls = self._patch(monkeypatch, self.SID, returncode=returncode, error=error)
        token_cache.write(WS, "p", _jwt(time.time() + 3600))
        assert len(calls) == 1
        assert list(token_cache.CACHE_DIR.glob("*.json")) == []
        assert not self._marker().exists()
        assert token_cache.lookup(WS, "p") is None


class TestLinkRefusalAndClearRetry:
    def test_read_refuses_symlink(self, tmp_path):
        target = tmp_path / "target"
        target.write_text("x")
        link = tmp_path / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        with pytest.raises(OSError):
            token_cache._read_text(link)
        with pytest.raises(OSError):
            token_cache._open_private(link, os.O_WRONLY | os.O_APPEND)

    def test_clear_retries_permission_error(self, monkeypatch):
        real = os.replace
        attempts = []

        def flaky(src, dst):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("busy")
            real(src, dst)

        monkeypatch.setattr(token_cache.os, "replace", flaky)
        monkeypatch.setattr(token_cache.time, "sleep", lambda s: None)
        token_cache.clear()
        assert len(attempts) == 3
        assert token_cache._cleared_at() > 0
