"""On-disk cache for the OAuth bearer `ug auth-token` prints.

Some agents resolve their API key by running `ug auth-token` before every request. Pi does so
uncached, kills the helper after 10s and discards its stderr. A miss costs the full ``ucode.cli``
import plus a `databricks auth token` subprocess that can stall on network I/O. A hit is answered
by ``ucode.entrypoint`` before any of that is imported, so this module must stay stdlib-only.

Only OAuth tokens minted through the Databricks CLI are cached: never PATs,
``DATABRICKS_BEARER`` / ``DATABRICKS_BEARER_COMMAND`` values, or custom-client tokens, and the
fast path defers to the full command when the saved workspace uses a PAT. Entries are keyed by
host, profile and the resolved ``DATABRICKS_CONFIG_FILE``. Files are ``0600`` inside a ``0700``
directory where the OS supports modes, like the CLI's own ``~/.databricks/token-cache.json``.
:func:`clear` records when it ran, and a fetch that started before then is not written back.

An entry is served while its JWT has at least ``MIN_REMAINING_SECONDS`` left. Once it is
``REFRESH_AFTER_SECONDS`` old it is still served, and the hit also starts a detached
``auth-token --refresh-cache`` that re-reads the token through the CLI and rewrites the entry, so
the periodic re-check never runs inside the caller's deadline. A login outside ug is picked up by
the next refresh; ug's own login path clears the cache at once.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".ucode" / "token-cache"
# ug's saved state, read (stdlib-only) for the PAT mode the full command would honor.
STATE_PATH = Path.home() / ".ucode" / "state.json"
_CLEARED_NAME = "cleared-at"
_LOCK_NAME = ".lock"
_LOCK_TIMEOUT_SECONDS = 2.0
# Never serve a token this close to expiry; the caller fetches through the CLI instead.
MIN_REMAINING_SECONDS = 300
# Re-check through the CLI, in the background, once an entry is this old.
REFRESH_AFTER_SECONDS = 1800
# Start at most one background refresh per entry in this window, so a CLI that keeps failing is
# retried without spawning a process per request.
REFRESH_RETRY_SECONDS = 300

_BEARER_ENV_VARS = ("DATABRICKS_BEARER", "DATABRICKS_BEARER_COMMAND")


def token_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim (epoch seconds), or None if ``token`` is not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None


def cache_applicable() -> bool:
    """False when an environment override owns auth; those tokens are never cached."""
    return not any(os.environ.get(name, "").strip() for name in _BEARER_ENV_VARS)


def _config_file_key() -> str:
    """The config file the CLI will read, as an absolute path; empty for its default.

    Another config file can map the same profile name to other credentials, and a relative path
    names a different file in each working directory."""
    config_file = os.environ.get("DATABRICKS_CONFIG_FILE", "")
    return os.path.abspath(os.path.expanduser(config_file)) if config_file else ""


def _entry_name(host: str, profile: str | None) -> str:
    key = "\0".join((host.strip().rstrip("/").lower(), profile or "", _config_file_key()))
    return hashlib.sha256(key.encode()).hexdigest()


def _cache_path(host: str, profile: str | None) -> Path:
    return CACHE_DIR / f"{_entry_name(host, profile)}.json"


def _refresh_marker_path(host: str, profile: str | None) -> Path:
    return CACHE_DIR / f"{_entry_name(host, profile)}.refresh"


def lookup(host: str, profile: str | None, *, now: float | None = None) -> tuple[str, bool] | None:
    """Return ``(token, needs_refresh)`` for a servable cached token, else None.

    ``needs_refresh`` is True once the entry is ``REFRESH_AFTER_SECONDS`` old, or when it is
    stamped in the future (the clock moved back)."""
    if not cache_applicable():
        return None
    now = time.time() if now is None else now
    try:
        entry = json.loads(_cache_path(host, profile).read_text(encoding="utf-8"))
        token = entry["token"]
        cached_at = float(entry["cached_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token:
        return None
    expires_at = token_expiry(token)
    if expires_at is None or now >= expires_at - MIN_REMAINING_SECONDS:
        return None
    return token, not 0 <= now - cached_at < REFRESH_AFTER_SECONDS


def read(host: str, profile: str | None, *, now: float | None = None) -> str | None:
    """Return a cached token that is still comfortably valid, else None."""
    hit = lookup(host, profile, now=now)
    return hit[0] if hit else None


def serve(host: str, profile: str | None) -> str | None:
    """Return the cached token, starting a background refresh if one is due."""
    hit = lookup(host, profile)
    if hit is None:
        return None
    token, needs_refresh = hit
    if needs_refresh:
        refresh_in_background(host, profile)
    return token


def write(
    host: str, profile: str | None, token: str, *, fetch_started: float | None = None
) -> None:
    """Atomically store ``token``, best-effort: a cache failure must never fail the helper.

    ``fetch_started`` is the ``time.time()`` at which the caller began fetching ``token``. If
    :func:`clear` ran since then (a login may have changed the identity), nothing is written."""
    if not cache_applicable() or token_expiry(token) is None:
        return
    path = _cache_path(host, profile)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "cached_at": time.time()}, fh)
        with _locked():
            if fetch_started is not None and _cleared_at() >= fetch_started:
                tmp.unlink()
                return
            os.replace(tmp, path)
    except (OSError, TimeoutError):
        try:
            tmp.unlink()
        except OSError:
            pass


def clear() -> None:
    """Drop every cached token, e.g. after a fresh `databricks auth login`.

    Also records when it ran, so a fetch already in flight (another helper process, or the
    background refresher) cannot write a token from before the login back afterwards. With no
    cache directory yet there is nothing to drop, and nothing is created."""
    if not CACHE_DIR.is_dir():
        return
    try:
        with _locked():
            _clear_entries()
    except TimeoutError:
        # A stuck lock holder must not keep a login from invalidating the cache.
        _clear_entries()
    except OSError:
        pass


def _clear_entries() -> None:
    stamp = CACHE_DIR / _CLEARED_NAME
    tmp = stamp.with_name(f".{stamp.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(repr(time.time()))
        os.replace(tmp, stamp)
    except OSError:
        pass
    try:
        entries = list(CACHE_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix in (".json", ".refresh", ".tmp"):
            try:
                entry.unlink()
            except OSError:
                pass


def _cleared_at() -> float:
    try:
        return float((CACHE_DIR / _CLEARED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return float("-inf")


class _locked:
    """Hold the cache's file lock, so a write's clear check and replace are one step."""

    def __enter__(self) -> None:
        from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock

        self._file = (CACHE_DIR / _LOCK_NAME).open("a+b")
        try:
            acquire_exclusive_file_lock(self._file, timeout=_LOCK_TIMEOUT_SECONDS)
        except BaseException:
            self._file.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        from ucode.os_compatibility.file_lock_cross_os import release_file_lock

        try:
            release_file_lock(self._file)
        finally:
            self._file.close()


def refresh_command(host: str, profile: str | None) -> list[str]:
    """Argv for the background refresher.

    ``-P`` keeps the working directory off ``sys.path``, so a directory that happens to contain a
    ``ucode`` package can't shadow the installed one. Like the `auth-token` argv agents run, it
    carries no workspace selection: the token belongs to the host."""
    argv = [sys.executable, "-P", "-m", "ucode", "auth-token", "--host", host]
    if profile:
        argv += ["--profile", profile]
    return [*argv, "--refresh-cache"]


def refresh_in_background(host: str, profile: str | None) -> bool:
    """Start a detached refresher for this entry; return whether one started.

    At most one starts per ``REFRESH_RETRY_SECONDS``. Failures are swallowed: the caller already
    has a token to serve."""
    try:
        if not _claim_refresh(_refresh_marker_path(host, profile), time.time()):
            return False
        _spawn_detached(refresh_command(host, profile))
    except Exception:
        return False
    return True


def _claim_refresh(marker: Path, now: float) -> bool:
    """Take this entry's refresh slot unless it was taken within the retry window.

    The marker is a throttle, not a lock: if two callers race past a stale marker, both start a
    refresher and both rewrite the entry atomically."""
    for _ in range(2):
        try:
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            return True
        except FileExistsError:
            pass
        try:
            age = now - marker.stat().st_mtime
        except FileNotFoundError:
            continue
        if 0 <= age < REFRESH_RETRY_SECONDS:
            return False
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    return False


def _spawn_detached(argv: list[str]) -> None:
    """Run ``argv`` in the background, detached from the caller.

    No stdio is inherited: pi waits until every holder of the helper's stdout pipe exits, so an
    inherited pipe would make it wait for the refresh too. A new session (POSIX) or process group
    without a console window (Windows) keeps the caller's signals and terminal off the refresher."""
    import subprocess

    from ucode.os_compatibility.subprocess_cross_os import popen

    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    popen(argv, **kwargs)


def _fast_path_target(args: list[str]) -> tuple[str, str | None] | None:
    if not args or args[0] != "auth-token":
        return None
    opts = args[1:]
    if len(opts) not in (2, 4, 6):
        return None
    parsed = dict(zip(opts[::2], opts[1::2], strict=True))
    if len(parsed) != len(opts) // 2 or not set(parsed) <= {
        "--host",
        "--profile",
        "--caller-timeout",
    }:
        return None
    host = parsed.get("--host", "").strip()
    if not host:
        return None
    return host, parsed.get("--profile") or None


def _saved_pat_mode() -> bool:
    """Whether the saved current workspace uses a PAT, which the full command honors for any host."""
    from ucode.state_file import current_workspace_state, read_state_file

    return bool(current_workspace_state(read_state_file(STATE_PATH)).get("use_pat"))


def fast_path_token(args: list[str]) -> str | None:
    """Serve ``auth-token --host H [--profile P] [--caller-timeout S]`` from the cache, else None.

    Only the exact OAuth argv ``build_auth_token_argv`` emits, plus pi's ``--caller-timeout``, is
    handled; a hit needs no time limit. Anything else
    (``--use-pat``, ``--force-refresh``, ``--browser-login``, ``--refresh-cache``, custom-client
    options, a missing ``--host``) returns None so the full CLI decides, as does a saved
    workspace in PAT mode."""
    target = _fast_path_target(args)
    if target is None or _saved_pat_mode():
        return None
    return serve(*target)
