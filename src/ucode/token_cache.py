"""On-disk cache for the OAuth bearer `ug auth-token` prints.

Some agents resolve their API key by running `ug auth-token` before every request. Pi does so
uncached, kills the helper after 10s and discards its stderr. A miss costs the full ``ucode.cli``
import plus a `databricks auth token` subprocess that can stall on network I/O. A hit is answered
by ``ucode.entrypoint`` before any of that is imported, so this module must stay stdlib-only.

Only OAuth tokens minted through the Databricks CLI are cached: never PATs,
``DATABRICKS_BEARER`` / ``DATABRICKS_BEARER_COMMAND`` values, or custom-client tokens. The cache is
off whenever an environment variable selects other credentials (``_IDENTITY_ENV_VARS``), and the
fast path defers to the full command when the saved workspace uses a PAT. Entries are keyed by
host, profile, ``XDG_CONFIG_HOME`` and the resolved config file, including its size and mtime, so
editing it in place drops its cached tokens. Files are ``0600`` inside a ``0700`` directory owned
by the user where the OS supports modes, like the CLI's own ``~/.databricks/token-cache.json``.
On Windows the directory is used only once icacls has limited it to SYSTEM, Administrators and
the user. Links are never followed, and a directory that fails those checks disables the cache.
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
import stat
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".ucode" / "token-cache"
# ug's saved state, read (stdlib-only) for the PAT mode the full command would honor.
STATE_PATH = Path.home() / ".ucode" / "state.json"
# Root-owned marker of a machine with a published agent configuration (managed_files owns it).
PUBLISHED_AGENT_CONFIG_MARKER_PATH = Path("/etc/unitygateway/agents-state.json")
_CLEARED_NAME = "cleared-at"
_LOCK_NAME = ".lock"
# On Windows, present only once icacls has limited the directory to SYSTEM, Administrators and
# this user; without it nothing is read from or written to the directory.
_ACL_MARKER_NAME = ".acl-v1"
_LOCK_TIMEOUT_SECONDS = 2.0
# `clear` and `write` order their stamps under the lock, so a write after a clear is always
# stamped later, and a clear later than every write before it, even within one tick of a coarse
# clock (time.time() moves in about 16ms steps on Windows with Python 3.12).
_CLEAR_AFTER_STAMP_SECONDS = 0.002
_WRITE_AFTER_CLEAR_SECONDS = 0.001
# A served entry's stamp may sit this far ahead of now (just after a clear, see above) without
# counting as a clock that moved back.
_FUTURE_STAMP_TOLERANCE_SECONDS = 1.0
# Never serve a token this close to expiry; the caller fetches through the CLI instead.
MIN_REMAINING_SECONDS = 300
# Re-check through the CLI, in the background, once an entry is this old.
REFRESH_AFTER_SECONDS = 1800
# Start at most one background refresh per entry in this window, so a CLI that keeps failing is
# retried without spawning a process per request.
REFRESH_RETRY_SECONDS = 300

# Each selects credentials other than the profile's CLI OAuth login, or changes where the CLI keeps
# it, so a token cached for the same host and profile could belong to another identity.
_IDENTITY_ENV_VARS = (
    "DATABRICKS_BEARER",
    "DATABRICKS_BEARER_COMMAND",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_AUTH_TYPE",
    "DATABRICKS_AUTH_STORAGE",
    "DATABRICKS_OIDC_TOKEN",
    "DATABRICKS_OIDC_TOKEN_FILEPATH",
    "DATABRICKS_GOOGLE_SERVICE_ACCOUNT",
    "DATABRICKS_GOOGLE_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "ARM_CLIENT_ID",
    "ARM_CLIENT_SECRET",
    "ARM_TENANT_ID",
    "ARM_USE_MSI",
    "DATABRICKS_AZURE_RESOURCE_ID",
    "DATABRICKS_USERNAME",
    "DATABRICKS_PASSWORD",
)
# Never follow a planted link to read or write a cache file (no-op where unsupported).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
# Well-known SIDs, because Windows translates the group names (SYSTEM, Administrators).
_WINDOWS_PRIVATE_GRANTS = ("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")


def token_expiry(token: str) -> float | None:
    """Return the JWT ``exp`` claim (epoch seconds), or None if ``token`` is not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None


def cache_applicable() -> bool:
    """False when an environment variable selects the credentials; those are never cached."""
    return not any(os.environ.get(name, "").strip() for name in _IDENTITY_ENV_VARS)


def _config_file_key() -> str:
    """The config file the CLI will read, as an absolute path; empty for its default.

    Another config file can map the same profile name to other credentials, and a relative path
    names a different file in each working directory."""
    config_file = os.environ.get("DATABRICKS_CONFIG_FILE", "")
    return os.path.abspath(os.path.expanduser(config_file)) if config_file else ""


def _config_file_version() -> str:
    """The config file's mtime and size, so an in-place edit keys new entries; empty if unread."""
    config_file = _config_file_key()
    path = Path(config_file) if config_file else Path.home() / ".databrickscfg"
    try:
        info = path.stat()
    except (OSError, ValueError):
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"


def _normalized_host(host: str) -> str:
    host = host.strip().lower()
    if "://" not in host:
        host = "https://" + host
    return host.rstrip("/")


def config_fingerprint() -> str:
    """The config file, ``XDG_CONFIG_HOME`` and the file's mtime and size that key an entry.

    Taken before a fetch and passed to :func:`write`, it keeps a token fetched under one
    config from being stored under the key of a config edited while the fetch ran."""
    return "\0".join(
        (_config_file_key(), os.environ.get("XDG_CONFIG_HOME", ""), _config_file_version())
    )


def _entry_name(host: str, profile: str | None) -> str:
    key = "\0".join((_normalized_host(host), profile or "", config_fingerprint()))
    return hashlib.sha256(key.encode()).hexdigest()


def _ensure_private_cache_dir(*, create: bool) -> bool:
    """Whether ``CACHE_DIR`` is a real directory private to this user, creating it if asked.

    A link, another user's directory, or one that can't be checked disables the cache. On POSIX,
    group or other permission bits are removed. On Windows the directory, new or pre-existing, is
    limited to SYSTEM, Administrators and the current user before its first write, and recorded
    as such by ``_ACL_MARKER_NAME``; until then (or if that fails) the cache is off."""
    try:
        if create:
            CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
            try:
                CACHE_DIR.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = CACHE_DIR.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return False
        if os.name != "nt":
            if info.st_uid != os.getuid():
                return False
            if info.st_mode & 0o077:
                os.chmod(CACHE_DIR, 0o700)
    except OSError:
        return False
    if sys.platform == "win32":
        return _windows_dir_restricted(create=create)
    return True


def _windows_dir_restricted(*, create: bool) -> bool:
    """Whether ``CACHE_DIR`` carries the ACL marker, restricting it first if ``create``."""
    marker = CACHE_DIR / _ACL_MARKER_NAME
    try:
        if stat.S_ISREG(marker.lstat().st_mode):
            return True
        if not create:
            return False
    except FileNotFoundError:
        if not create:
            return False
    except OSError:
        return False
    if not _restrict_windows_dir(CACHE_DIR):
        return False
    try:
        os.close(_open_private(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
    except OSError:
        return False
    return True


def _current_user_sid() -> str:
    """Return this process's user SID string, or "" when it can't be resolved."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return ""
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not size.value:
                return ""
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                return ""
            # TOKEN_USER begins with a SID_AND_ATTRIBUTES whose first field is the SID pointer.
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            out = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
                return ""
            try:
                return out.value or ""
            finally:
                kernel32.LocalFree(out)
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return ""


def _refuse_link(path: Path) -> None:
    """Raise OSError if ``path`` is a symlink (Windows lacks ``O_NOFOLLOW``)."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise OSError(f"refusing to open link: {path}")


def _restrict_windows_dir(path: Path) -> bool:
    """Replace inherited access to ``path`` with SYSTEM, Administrators and this user.

    Return whether icacls succeeded. Without this user's SID nothing is changed (stripping
    inheritance could lock this user out) and the result is False."""
    try:
        from ucode.os_compatibility.subprocess_cross_os import run
        from ucode.os_compatibility.system_cross_os import windows_system32_dir

        sid = _current_user_sid()
        if not sid.startswith("S-1-"):
            return False
        grants = [*_WINDOWS_PRIVATE_GRANTS, f"*{sid}:(OI)(CI)F"]
        result = run(
            [
                str(windows_system32_dir() / "icacls.exe"),
                str(path),
                "/L",
                "/inheritance:r",
                *(arg for grant in grants for arg in ("/grant:r", grant)),
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _open_private(path: Path, flags: int) -> int:
    """Open ``path`` without following a link, as ``0600`` on POSIX even if it already exists."""
    _refuse_link(path)
    fd = os.open(path, flags | _O_NOFOLLOW | _O_BINARY, 0o600)
    if os.name != "nt":
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
    return fd


def _read_text(path: Path) -> str:
    """Read a cache file, refusing a link."""
    _refuse_link(path)
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode("utf-8")


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` (which must not exist) holding ``text``."""
    fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))


def _cache_path(host: str, profile: str | None) -> Path:
    return CACHE_DIR / f"{_entry_name(host, profile)}.json"


def _refresh_marker_path(host: str, profile: str | None) -> Path:
    return CACHE_DIR / f"{_entry_name(host, profile)}.refresh"


def lookup(host: str, profile: str | None, *, now: float | None = None) -> tuple[str, bool] | None:
    """Return ``(token, needs_refresh)`` for a servable cached token, else None.

    ``needs_refresh`` is True once the entry is ``REFRESH_AFTER_SECONDS`` old, or when it is
    stamped in the future (the clock moved back). An entry written before the latest
    :func:`clear` is a miss even if deleting it failed (on Windows a concurrent reader can
    block the delete), so a login's previous identity is never served."""
    if not cache_applicable() or not _ensure_private_cache_dir(create=False):
        return None
    now = time.time() if now is None else now
    try:
        entry = json.loads(_read_text(_cache_path(host, profile)))
        token = entry["token"]
        cached_at = float(entry["cached_at"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None
    if not isinstance(token, str) or not token or cached_at <= _cleared_at():
        return None
    expires_at = token_expiry(token)
    if expires_at is None or now >= expires_at - MIN_REMAINING_SECONDS:
        return None
    age = now - cached_at
    return token, not -_FUTURE_STAMP_TOLERANCE_SECONDS <= age < REFRESH_AFTER_SECONDS


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
    host: str,
    profile: str | None,
    token: str,
    *,
    fetch_started: float | None = None,
    expected_config: str | None = None,
) -> None:
    """Atomically store ``token``, best-effort: a cache failure must never fail the helper.

    ``fetch_started`` is the ``time.time()`` at which the caller began fetching ``token``. If
    :func:`clear` ran since then (a login may have changed the identity), nothing is written.
    ``expected_config`` is :func:`config_fingerprint` from before the fetch; if the config has
    changed since, the token may belong to the old config's identity and is not written.

    A :func:`clear` that times out on the lock clears without it, so it can land while this
    write holds the lock. The entry is stamped with the time before it was written and checked
    against the clear stamp again after the replace; one that a clear overtook is removed, or,
    if that fails, the clear stamp is moved past it."""
    if not cache_applicable() or token_expiry(token) is None:
        return
    if not _ensure_private_cache_dir(create=True):
        return
    path = _cache_path(host, profile)
    tmp = _temp_path(path)
    try:
        with _locked():
            if expected_config is not None and config_fingerprint() != expected_config:
                return
            write_started = time.time()
            cleared_before = _cleared_at()
            if fetch_started is not None and cleared_before >= fetch_started:
                return
            # Stamped after the latest clear, which `lookup` requires of a servable entry.
            cached_at = max(write_started, cleared_before + _WRITE_AFTER_CLEAR_SECONDS)
            _write_new(tmp, json.dumps({"token": token, "cached_at": cached_at}))
            os.replace(tmp, path)
            cleared_after = _cleared_at()
            if (
                cleared_after != cleared_before
                or cleared_after >= cached_at
                or (fetch_started is not None and cleared_after >= fetch_started)
            ):
                _discard_overtaken(path, cached_at)
    except (OSError, TimeoutError):
        try:
            tmp.unlink()
        except OSError:
            pass


def _discard_overtaken(path: Path, cached_at: float) -> None:
    """Remove an entry a concurrent clear overtook, or move the clear stamp past it."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _write_cleared_at(max(time.time(), cached_at))


def clear() -> None:
    """Drop every cached token, e.g. after a fresh `databricks auth login`.

    Also records when it ran, so a fetch already in flight (another helper process, or the
    background refresher) cannot write a token from before the login back afterwards. That
    includes a first fetch that has not created the cache directory yet, so the directory is
    created to hold the record. A directory that is not private to this user is left alone:
    nothing in it is ever served."""
    if not _ensure_private_cache_dir(create=True):
        return
    try:
        with _locked():
            _clear_entries()
    except (TimeoutError, OSError):
        # A stuck lock holder, or a lock file that can't be opened (a link, another user's),
        # must not keep a login from invalidating the cache.
        _clear_entries()


def _clear_entries() -> None:
    # Later than every entry written before now (see `write`), even within one clock tick.
    _write_cleared_at(max(time.time(), _cleared_at() + _CLEAR_AFTER_STAMP_SECONDS))
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


def _write_cleared_at(cleared_at: float) -> None:
    """Atomically replace the clear stamp, best-effort."""
    stamp = CACHE_DIR / _CLEARED_NAME
    tmp = _temp_path(stamp)
    try:
        _write_new(tmp, repr(cleared_at))
        for attempt in range(3):
            try:
                os.replace(tmp, stamp)
                break
            except PermissionError:
                # A concurrent Windows reader of `cleared-at` can briefly block the replace.
                if attempt == 2:
                    raise
                time.sleep(0.005)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _cleared_at() -> float:
    try:
        return float(_read_text(CACHE_DIR / _CLEARED_NAME))
    except (OSError, ValueError, OverflowError):
        return float("-inf")


class _locked:
    """Hold the cache's file lock, so a write's clear check and replace are one step."""

    def __enter__(self) -> None:
        from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock

        fd = _open_private(CACHE_DIR / _LOCK_NAME, os.O_RDWR | os.O_CREAT)
        self._file = os.fdopen(fd, "r+b")
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
    """Use the marker's stamped clock; filesystem mtimes may run ahead."""
    for _ in range(2):
        try:
            fd = _open_private(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            pass
        else:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(repr(now).encode())
            except OSError:
                pass  # the claim stands; its age falls back to the file's mtime
            return True
        claimed_at = _claimed_at(marker)
        if claimed_at is None:
            continue
        # A file-time fallback can sit a little ahead of `now` (NTFS stamps from a finer clock).
        if -_FUTURE_STAMP_TOLERANCE_SECONDS <= now - claimed_at < REFRESH_RETRY_SECONDS:
            return False
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    return False


def _claimed_at(marker: Path) -> float | None:
    """When the marker's claim was taken, or None if the marker is gone.

    A marker without a readable stamp (its writer stopped after creating it, or an older ug
    wrote it empty) falls back to the file's mtime."""
    try:
        return float(_read_text(marker))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, OverflowError):
        pass
    try:
        return marker.lstat().st_mtime
    except FileNotFoundError:
        return None


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


def _workspace_url(url: str) -> str:
    """Stdlib mirror of ``ui.normalize_workspace_url``."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


def _published_agent_config_present() -> bool:
    """Stdlib mirror of ``managed_files.published_agent_config_used``."""
    if not (sys.platform == "darwin" or sys.platform.startswith("linux")):
        return False
    try:
        info = PUBLISHED_AGENT_CONFIG_MARKER_PATH.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        return False
    try:
        data = json.loads(PUBLISHED_AGENT_CONFIG_MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(data, dict)


def _defers_to_saved_choices(host: str, profile: str | None) -> bool:
    """Whether the full command would take its choices from the saved workspace for ``host``.

    With ``--host`` it does so only on a machine with a published agent configuration when the
    saved workspace matches ``host``; a saved PAT mode then always applies, and a saved custom
    OAuth client or profile applies when ``--profile`` was omitted."""
    if not _published_agent_config_present():
        return False
    from ucode.state_file import current_workspace_state, read_state_file

    saved = current_workspace_state(read_state_file(STATE_PATH))
    saved_ws = saved.get("workspace")
    if (
        not isinstance(saved_ws, str)
        or not saved_ws.strip()
        or _workspace_url(saved_ws) != _workspace_url(host)
    ):
        return False
    saved_oauth = saved.get("custom_oauth")
    has_saved_oauth = (
        isinstance(saved_oauth, dict)
        and isinstance(saved_oauth.get("client_id"), str)
        and isinstance(saved_oauth.get("scopes"), list)
    )
    return bool(
        saved.get("use_pat") or (profile is None and (has_saved_oauth or saved.get("profile")))
    )


def fast_path_token(args: list[str]) -> str | None:
    """Serve ``auth-token --host H [--profile P] [--caller-timeout S]`` from the cache, else None.

    Only the exact OAuth argv ``build_auth_token_argv`` emits, plus pi's ``--caller-timeout``, is
    handled; a hit needs no time limit. Anything else
    (``--use-pat``, ``--force-refresh``, ``--browser-login``, ``--refresh-cache``, custom-client
    options, a missing ``--host``) returns None so the full CLI decides, as does a host whose
    saved choices the full command would apply (see ``_defers_to_saved_choices``)."""
    target = _fast_path_target(args)
    if target is None:
        return None
    host, profile = target
    if _defers_to_saved_choices(host, profile):
        return None
    return serve(host, profile)
