"""Bounded Databricks authentication for Claude Desktop/Cowork.

Claude Desktop invokes its credential helper repeatedly and treats stdout as the
credential.  Keep this module deliberately small: the existing Databricks CLI
owns token caching and refresh, while this module controls when an interactive
browser login is allowed and keeps its diagnostics off the token stream.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NoReturn

from ucode.config_io import APP_DIR
from ucode.databricks import (
    build_databricks_cli_env,
    databricks_cli_path,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.os_compatibility.file_lock_cross_os import (
    acquire_exclusive_file_lock,
    release_file_lock,
)

# Claude Desktop's helper cache is intentionally shorter than a typical OAuth
# access-token lifetime.  These constants are also used by the CLI when it
# writes the desktop helper configuration.
CLAUDE_DESKTOP_AUTH_TTL_SECONDS = 900
CLAUDE_DESKTOP_AUTH_TTL_MS = CLAUDE_DESKTOP_AUTH_TTL_SECONDS * 1000
CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS = 300
CLAUDE_DESKTOP_AUTH_TIMEOUT_MS = CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS * 1000
CLAUDE_DESKTOP_AUTH_SILENT_REFRESH = True
CLAUDE_HELPER_CONTEXT_ENV = "CLAUDE_HELPER_CONTEXT"
CLAUDE_HELPER_INTERACTIVE_CONTEXT = "interactive"

_TOKEN_TIMEOUT_SECONDS = 15
_HELPER_BUDGET_SECONDS = 270
_AUTH_LOCK_PATH = APP_DIR / "claude-desktop-auth.lock"
_NETWORK_MARKERS = (
    "network",
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "could not resolve",
    "no such host",
    "temporary failure",
    "temporarily unavailable",
    "dns",
    "proxy",
    "tls",
    "certificate",
    "http 5",
    "status code 5",
)

TokenFailureKind = Literal["auth", "network", "cli", "malformed"]


class ClaudeDesktopAuthError(RuntimeError):
    """Actionable failure returned by the Claude Desktop credential helper."""

    def __init__(self, message: str, *, kind: TokenFailureKind = "auth") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class _TokenAttempt:
    token: str | None
    failure: TokenFailureKind | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.token is not None


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _safe_detail(value: object) -> str:
    """Keep CLI diagnostics useful without copying credential-looking values."""
    detail = " ".join(_as_text(value).split())
    if not detail:
        return ""
    # The CLI normally does not echo tokens, but avoid forwarding one if a
    # wrapper or a future CLI version includes it in an error.
    words = detail.split()
    for index, word in enumerate(words[:-1]):
        if word.rstrip(":=").lower() in {
            "token",
            "access_token",
            "access-token",
            "bearer",
        }:
            words[index + 1] = "<redacted>"
    return " ".join(words)[:240]


def _is_network_detail(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _NETWORK_MARKERS)


def _failure_from_exception(exc: BaseException) -> tuple[TokenFailureKind, str]:
    if isinstance(exc, FileNotFoundError):
        return "cli", "Databricks CLI was not found"
    if isinstance(exc, PermissionError):
        return "cli", "Databricks CLI could not be executed"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "network", "Databricks CLI timed out"
    detail = _safe_detail(exc)
    if _is_network_detail(detail):
        return "network", detail
    if isinstance(exc, OSError):
        return "cli", detail or "Databricks CLI could not be executed"
    return "auth", detail


def _resolve_cli_path() -> str:
    """Return the absolute CLI path used by every helper subprocess."""
    candidate = databricks_cli_path()
    if os.path.isabs(candidate):
        return candidate
    resolved = shutil.which(candidate)
    if resolved:
        return os.path.abspath(resolved)
    raise ClaudeDesktopAuthError(
        "Databricks CLI was not found. Install it, then start a new Cowork task and try again.",
        kind="cli",
    )


def _profile_args(profile: str | None) -> list[str]:
    # Do not discover a profile from the host.  Desktop state chooses the
    # profile; an omitted profile means the CLI's configured default profile.
    return ["--profile", profile] if profile else []


def _token_command(
    cli_path: str,
    host: str,
    profile: str | None,
    *,
    force_refresh: bool,
) -> list[str]:
    command = [
        cli_path,
        "auth",
        "token",
        "--host",
        host,
        *_profile_args(profile),
        "--output",
        "json",
    ]
    if force_refresh:
        command.append("--force-refresh")
    return command


def _login_command(
    cli_path: str,
    host: str,
    profile: str | None,
) -> list[str]:
    return [cli_path, "auth", "login", "--host", host, *_profile_args(profile)]


def _read_token(result: object) -> _TokenAttempt:
    returncode = getattr(result, "returncode", 1)
    stdout = _as_text(getattr(result, "stdout", ""))
    stderr = _as_text(getattr(result, "stderr", ""))
    detail = _safe_detail(stderr or stdout)
    if returncode != 0:
        return _TokenAttempt(
            None,
            "network" if _is_network_detail(detail) else "auth",
            detail,
        )

    try:
        payload = json.loads(stdout or "")
    except (TypeError, ValueError):
        return _TokenAttempt(None, "malformed", "Databricks CLI returned invalid token JSON")
    if not isinstance(payload, dict):
        return _TokenAttempt(None, "malformed", "Databricks CLI returned invalid token JSON")

    raw_token = payload.get("access_token")
    if not isinstance(raw_token, str):
        return _TokenAttempt(None, "malformed", "Databricks CLI returned no access token")
    token = raw_token.strip()
    if not token or any(character.isspace() or ord(character) < 0x20 for character in token):
        return _TokenAttempt(None, "malformed", "Databricks CLI returned an invalid access token")
    return _TokenAttempt(token)


def _fetch_token(
    cli_path: str,
    host: str,
    profile: str | None,
    *,
    force_refresh: bool,
    timeout: float,
) -> _TokenAttempt:
    try:
        result = subprocess_cross_os.run(
            _token_command(cli_path, host, profile, force_refresh=force_refresh),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=build_databricks_cli_env(host, profile),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure, detail = _failure_from_exception(exc)
        return _TokenAttempt(None, failure, detail)
    return _read_token(result)


def _run_login(
    cli_path: str,
    host: str,
    profile: str | None,
    *,
    timeout: float,
) -> _TokenAttempt:
    """Run login with all CLI output routed to stderr, never helper stdout."""
    try:
        result = subprocess_cross_os.run(
            _login_command(cli_path, host, profile),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env=build_databricks_cli_env(host, profile),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failure, detail = _failure_from_exception(exc)
        return _TokenAttempt(None, failure, detail)

    returncode = getattr(result, "returncode", 1)
    if returncode == 0:
        return _TokenAttempt(None)
    return _TokenAttempt(None, "auth", f"Databricks login exited with status {returncode}")


def _run_with_interactive_login_lock(deadline: float, callback: Callable[[], str]) -> str:
    """Run browser recovery under a cross-process lock with a bounded wait."""
    try:
        _AUTH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    except ClaudeDesktopAuthError:
        raise
    except OSError as exc:
        raise ClaudeDesktopAuthError(
            "Could not coordinate Databricks sign-in. Start a new Cowork task and try again.",
            kind="cli",
        ) from exc

    try:
        with _AUTH_LOCK_PATH.open("a+b") as lock_file:
            try:
                acquire_exclusive_file_lock(
                    lock_file,
                    timeout=_remaining_budget(deadline, _HELPER_BUDGET_SECONDS),
                )
            except TimeoutError as exc:
                raise ClaudeDesktopAuthError(
                    "Timed out waiting for another Databricks sign-in. Start a new Cowork task and try again.",
                    kind="network",
                ) from exc
            try:
                return callback()
            finally:
                release_file_lock(lock_file)
    except ClaudeDesktopAuthError:
        raise
    except OSError as exc:
        raise ClaudeDesktopAuthError(
            "Could not coordinate Databricks sign-in. Start a new Cowork task and try again.",
            kind="cli",
        ) from exc


def _interactive_context() -> bool:
    return os.environ.get(CLAUDE_HELPER_CONTEXT_ENV, "").strip().lower() == (
        CLAUDE_HELPER_INTERACTIVE_CONTEXT
    )


def _remaining_budget(deadline: float, requested: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ClaudeDesktopAuthError(
            "Databricks sign-in timed out. Start a new Cowork task and try again.",
            kind="network",
        )
    return min(requested, remaining)


def _raise_failure(
    attempt: _TokenAttempt,
    *,
    interactive: bool,
    after_browser: bool = False,
) -> NoReturn:
    kind = attempt.failure or "auth"
    if kind == "cli":
        raise ClaudeDesktopAuthError(
            "Databricks CLI could not be run. Install or repair it, then start a new Cowork task.",
            kind="cli",
        )
    if kind == "network":
        raise ClaudeDesktopAuthError(
            "Could not reach Databricks while refreshing credentials. Check your network and "
            "retry the Cowork task.",
            kind="network",
        )
    if after_browser:
        raise ClaudeDesktopAuthError(
            "Databricks sign-in did not complete. Start a new Cowork task and sign in again.",
            kind="auth",
        )
    if not interactive:
        raise ClaudeDesktopAuthError(
            "Databricks authentication expired. Start a new Cowork task to sign in, then retry.",
            kind="auth",
        )
    raise ClaudeDesktopAuthError(
        "Databricks authentication is unavailable. Start a new Cowork task and sign in again.",
        kind="auth",
    )


def get_claude_desktop_token(
    host: str,
    profile: str | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    """Return one access token for Claude Desktop's ``apiKeyHelper``.

    ``profile`` comes from configured ucode state and is passed explicitly when
    present.  This function never searches for a profile by host.  It first
    attempts the Databricks CLI's cached/silent-refresh path.  Browser login is
    allowed only when ``CLAUDE_HELPER_CONTEXT=interactive``; all other callers
    receive a message directing them to start a new Cowork task.
    """
    if not isinstance(host, str) or not host.strip() or any(c in host for c in "\r\n"):
        raise ClaudeDesktopAuthError("A valid Databricks workspace host is required.")
    host = host.strip()
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip() or any(c in profile for c in "\r\n"):
            raise ClaudeDesktopAuthError("The configured Databricks profile is invalid.")
        profile = profile.strip()

    cli_path = _resolve_cli_path()
    started = time.monotonic()
    deadline = started + _HELPER_BUDGET_SECONDS
    first = _fetch_token(
        cli_path,
        host,
        profile,
        force_refresh=force_refresh,
        timeout=min(_TOKEN_TIMEOUT_SECONDS, _remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS)),
    )
    if first.ok:
        assert first.token is not None
        return first.token
    if first.failure in {"cli", "network"}:
        _raise_failure(first, interactive=False)

    interactive = _interactive_context()
    if not interactive:
        _raise_failure(first, interactive=False)

    # Serialize the browser branch.  A second helper that arrives while the
    # first is in the browser rechecks the token after taking this lock and
    # therefore does not open another browser window.
    def recover_interactively() -> str:
        locked_retry = _fetch_token(
            cli_path,
            host,
            profile,
            force_refresh=force_refresh,
            timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
        )
        if locked_retry.ok:
            assert locked_retry.token is not None
            return locked_retry.token
        if locked_retry.failure in {"cli", "network"}:
            _raise_failure(locked_retry, interactive=True)

        browser = _run_login(
            cli_path,
            host,
            profile,
            timeout=_remaining_budget(deadline, CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS),
        )
        if browser.failure is not None:
            _raise_failure(browser, interactive=True, after_browser=True)
        final = _fetch_token(
            cli_path,
            host,
            profile,
            force_refresh=force_refresh,
            timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
        )
        if final.ok:
            assert final.token is not None
            return final.token
        _raise_failure(final, interactive=True, after_browser=True)

    return _run_with_interactive_login_lock(deadline, recover_interactively)


__all__ = [
    "CLAUDE_DESKTOP_AUTH_SILENT_REFRESH",
    "CLAUDE_DESKTOP_AUTH_TIMEOUT_MS",
    "CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS",
    "CLAUDE_DESKTOP_AUTH_TTL_MS",
    "CLAUDE_DESKTOP_AUTH_TTL_SECONDS",
    "CLAUDE_HELPER_CONTEXT_ENV",
    "CLAUDE_HELPER_INTERACTIVE_CONTEXT",
    "ClaudeDesktopAuthError",
    "get_claude_desktop_token",
]
