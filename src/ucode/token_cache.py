"""On-disk cache for the OAuth bearer `ug auth-token` prints.

Some agents resolve their API key by running `ug auth-token` before every request. Pi does so
uncached, kills the helper after 10s and discards its stderr. A miss costs the full ``ucode.cli``
import plus a `databricks auth token` subprocess that can stall on network I/O. A hit is answered
by ``ucode.entrypoint`` before any of that is imported, so this module must stay stdlib-only.

Only OAuth tokens minted through the Databricks CLI are cached: never PATs,
``DATABRICKS_BEARER`` / ``DATABRICKS_BEARER_COMMAND`` values, or custom-client tokens. Entries are
keyed by host, profile and ``DATABRICKS_CONFIG_FILE``. Files are ``0600`` inside a ``0700``
directory where the OS supports modes, like the CLI's own ``~/.databricks/token-cache.json``.

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
import shutil
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".ucode" / "token-cache"
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


def _entry_name(host: str, profile: str | None) -> str:
    key = "\0".join(
        (
            host.strip().rstrip("/").lower(),
            profile or "",
            # Another config file can map the same profile name to other credentials.
            os.environ.get("DATABRICKS_CONFIG_FILE", ""),
        )
    )
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


def write(host: str, profile: str | None, token: str) -> None:
    """Atomically store ``token``, best-effort: a cache failure must never fail the helper."""
    if not cache_applicable() or token_expiry(token) is None:
        return
    path = _cache_path(host, profile)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "cached_at": time.time()}, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def clear() -> None:
    """Drop every cached token, e.g. after a fresh `databricks auth login`."""
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


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
    if len(opts) not in (2, 4):
        return None
    parsed = dict(zip(opts[::2], opts[1::2], strict=True))
    if len(parsed) != len(opts) // 2 or not set(parsed) <= {"--host", "--profile"}:
        return None
    host = parsed.get("--host", "").strip()
    if not host:
        return None
    return host, parsed.get("--profile") or None


def fast_path_token(args: list[str]) -> str | None:
    """Serve ``auth-token --host H [--profile P]`` from the cache, else None.

    Only the exact OAuth argv ``build_auth_token_argv`` emits is handled. Anything else
    (``--use-pat``, ``--force-refresh``, ``--browser-login``, ``--refresh-cache``, custom-client
    options, a missing ``--host``) returns None so the full CLI decides."""
    target = _fast_path_target(args)
    return serve(*target) if target else None
