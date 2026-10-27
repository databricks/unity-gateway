"""Bound Desktop browser recovery while the Databricks CLI owns token caching/refresh."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from ucode.config_io import APP_DIR
from ucode.databricks import (
    build_databricks_cli_env,
    databricks_cli_path,
    external_bearer_configured,
    get_databricks_token,
    prepare_databricks_login_env,
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
CLAUDE_CODE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

AI_GATEWAY_TOKEN_HEADER = "X-Databricks-AI-Gateway-Token"

_TOKEN_TIMEOUT_SECONDS = 15
_HELPER_BUDGET_SECONDS = 270
_AUTH_LOCK_PATH = APP_DIR / "claude-desktop-auth.lock"
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CLAUDE_CREDENTIALS_FILENAME = ".credentials.json"
_CLAUDE_CREDENTIALS_INVALID_MESSAGE = (
    "Claude subscription credentials are invalid. "
    "Run `claude auth login`, then start a new Cowork task."
)
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

TokenFailureKind = Literal["auth", "network", "cli", "config"]
_FAILURE_MESSAGES = {
    "auth": "Databricks authentication expired. Start a new Cowork task to sign in, then retry.",
    "network": (
        "Could not reach Databricks while refreshing credentials. Check your network and "
        "retry the Cowork task."
    ),
    "cli": "Databricks CLI could not be run. Install or repair it, then start a new Cowork task.",
    "config": "Credential helper configuration is invalid.",
}


class ClaudeDesktopAuthError(RuntimeError):
    """Actionable failure returned by the Claude Desktop credential helper."""

    def __init__(self, message: str | None = None, *, kind: TokenFailureKind = "auth") -> None:
        super().__init__(_FAILURE_MESSAGES[kind] if message is None else message)
        self.kind = kind


def _as_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _is_network_detail(value: object) -> bool:
    words = _as_text(value).split()
    # Preserve classification without matching credential values as diagnostics.
    for index, word in enumerate(words[:-1]):
        if word.rstrip(":=").lower() in {
            "token",
            "access_token",
            "access-token",
            "bearer",
        }:
            words[index + 1] = "<redacted>"
    detail = " ".join(words)[:240].lower()
    return any(marker in detail for marker in _NETWORK_MARKERS)


def _subprocess_error(exc: OSError | subprocess.TimeoutExpired) -> ClaudeDesktopAuthError:
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return ClaudeDesktopAuthError(kind="cli")
    if isinstance(exc, subprocess.TimeoutExpired) or _is_network_detail(exc):
        return ClaudeDesktopAuthError(kind="network")
    return ClaudeDesktopAuthError(kind="cli")


def _profile_args(profile: str | None) -> list[str]:
    # Do not discover a profile from the host.  Desktop state chooses the
    # profile; an omitted profile means the CLI's configured default profile.
    return ["--profile", profile] if profile else []


def _read_token(result: subprocess.CompletedProcess[str]) -> str:
    stdout = _as_text(result.stdout)
    if result.returncode != 0:
        raise ClaudeDesktopAuthError(
            kind="network" if _is_network_detail(result.stderr or stdout) else "auth"
        )

    try:
        payload = json.loads(stdout or "")
    except (TypeError, ValueError):
        raise ClaudeDesktopAuthError() from None
    if not isinstance(payload, dict):
        raise ClaudeDesktopAuthError()

    raw_token = payload.get("access_token")
    if not isinstance(raw_token, str):
        raise ClaudeDesktopAuthError()
    token = raw_token.strip()
    if not token or any(character.isspace() or ord(character) < 0x20 for character in token):
        raise ClaudeDesktopAuthError()
    return token


def _fetch_token(
    cli_path: str,
    host: str,
    profile: str | None,
    *,
    force_refresh: bool,
    timeout: float,
) -> str:
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
    try:
        result = subprocess_cross_os.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=build_databricks_cli_env(host, profile),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _subprocess_error(exc) from None
    return _read_token(result)


def _run_login(
    cli_path: str,
    host: str,
    profile: str | None,
    *,
    timeout: float,
) -> None:
    """Run login with all CLI output routed to stderr, never helper stdout."""
    try:
        env = prepare_databricks_login_env(host, profile)
    except RuntimeError as exc:
        raise ClaudeDesktopAuthError(str(exc), kind="config") from None
    try:
        result = subprocess_cross_os.run(
            [cli_path, "auth", "login", "--host", host, *_profile_args(profile)],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _subprocess_error(exc) from None

    if result.returncode != 0:
        raise ClaudeDesktopAuthError()


@contextmanager
def _auth_lock(deadline: float) -> Iterator[None]:
    try:
        _AUTH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUTH_LOCK_PATH.open("a+b") as lock_file:
            try:
                acquire_exclusive_file_lock(
                    lock_file,
                    timeout=_remaining_budget(deadline, _HELPER_BUDGET_SECONDS),
                )
            except TimeoutError as exc:
                raise ClaudeDesktopAuthError(
                    "Timed out waiting for another sign-in. Start a new Cowork task and try again.",
                    kind="network",
                ) from exc
            try:
                yield
            finally:
                release_file_lock(lock_file)
    except OSError as exc:
        raise ClaudeDesktopAuthError(
            "Could not coordinate sign-in. Start a new Cowork task and try again.",
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
            "Credential refresh timed out. Start a new Cowork task and try again.",
            kind="network",
        )
    return min(requested, remaining)


def get_claude_desktop_databricks_token(
    host: str,
    profile: str | None = None,
    *,
    force_refresh: bool = False,
) -> str:
    """Return one Databricks access token for Claude Desktop's ``apiKeyHelper``.

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

    if external_bearer_configured():
        return get_databricks_token(host, profile, force_refresh=force_refresh)

    cli_path = databricks_cli_path()
    if not os.path.isabs(cli_path):
        raise ClaudeDesktopAuthError(
            "Databricks CLI was not found. Install it, then start a new Cowork task and try again.",
            kind="cli",
        )
    deadline = time.monotonic() + _HELPER_BUDGET_SECONDS
    try:
        return _fetch_token(
            cli_path,
            host,
            profile,
            force_refresh=force_refresh,
            timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
        )
    except ClaudeDesktopAuthError as exc:
        if exc.kind != "auth" or not _interactive_context():
            raise

    with _auth_lock(deadline):
        # Another helper may have completed sign-in while we waited for the lock.
        try:
            return _fetch_token(
                cli_path,
                host,
                profile,
                force_refresh=force_refresh,
                timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
            )
        except ClaudeDesktopAuthError as exc:
            if exc.kind != "auth":
                raise

        try:
            _run_login(
                cli_path,
                host,
                profile,
                timeout=_remaining_budget(deadline, CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS),
            )
            return _fetch_token(
                cli_path,
                host,
                profile,
                force_refresh=force_refresh,
                timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
            )
        except ClaudeDesktopAuthError as exc:
            if exc.kind != "auth":
                raise
            raise ClaudeDesktopAuthError(
                "Databricks sign-in did not complete. Start a new Cowork task and sign in again."
            ) from None


def _claude_credentials_path() -> Path:
    if config_dir := os.environ.get(CLAUDE_CONFIG_DIR_ENV):
        return Path(config_dir).expanduser() / _CLAUDE_CREDENTIALS_FILENAME
    return Path.home() / ".claude" / _CLAUDE_CREDENTIALS_FILENAME


def _credential_storage_platform() -> str:
    return sys.platform


def _claude_keychain_service() -> str:
    if not (config_dir := os.environ.get(CLAUDE_CONFIG_DIR_ENV)):
        return _CLAUDE_KEYCHAIN_SERVICE
    normalized = unicodedata.normalize("NFC", config_dir)
    suffix = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return f"{_CLAUDE_KEYCHAIN_SERVICE}-{suffix}"


def _clean_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or any(character.isspace() or ord(character) < 0x20 for character in token):
        return None
    return token


def _read_claude_credentials() -> dict:
    platform = _credential_storage_platform()
    if platform == "darwin":
        account = os.environ.get("USER")
        command = [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            _claude_keychain_service(),
            "-w",
        ]
        if account:
            command[2:2] = ["-a", account]
        try:
            result = subprocess_cross_os.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=_TOKEN_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClaudeDesktopAuthError(
                "Could not read the Claude subscription credential from macOS Keychain.",
                kind="cli",
            ) from exc
        raw = _as_text(result.stdout)
        if result.returncode != 0:
            raise ClaudeDesktopAuthError(
                "Claude subscription authentication is required. Run `claude auth login`, then start a new Cowork task.",
                kind="auth",
            )
    elif platform in ("linux", "win32"):
        try:
            raw = _claude_credentials_path().read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ClaudeDesktopAuthError(
                "Claude subscription authentication is required. Run `claude auth login`, then start a new Cowork task.",
                kind="auth",
            ) from exc
    else:
        raise ClaudeDesktopAuthError(
            "Claude subscription authentication is unavailable on this platform.", kind="cli"
        )

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise ClaudeDesktopAuthError(_CLAUDE_CREDENTIALS_INVALID_MESSAGE, kind="auth") from None
    if not isinstance(payload, dict):
        raise ClaudeDesktopAuthError(_CLAUDE_CREDENTIALS_INVALID_MESSAGE, kind="auth")
    return payload


def _claude_subscription_token() -> str:
    # Support headless, managed, and automated setups that cannot use interactive local login.
    if (
        configured_token := os.environ.get(CLAUDE_CODE_OAUTH_TOKEN_ENV)
    ) and configured_token.strip():
        if token := _clean_token(configured_token):
            return token
        raise ClaudeDesktopAuthError(
            "CLAUDE_CODE_OAUTH_TOKEN is invalid. Unset it to use Claude's normal sign-in flow, "
            "or replace it with a valid token to continue using the environment override. "
            "Then start a new Cowork task.",
            kind="config",
        )

    payload = _read_claude_credentials()
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ClaudeDesktopAuthError(
            "Claude subscription authentication is required. Run `claude auth login`, then start a new Cowork task.",
            kind="auth",
        )
    token = _clean_token(oauth.get("accessToken"))
    expires_at = oauth.get("expiresAt")
    if token is None:
        raise ClaudeDesktopAuthError(_CLAUDE_CREDENTIALS_INVALID_MESSAGE, kind="auth")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ClaudeDesktopAuthError(_CLAUDE_CREDENTIALS_INVALID_MESSAGE, kind="auth")
    if isinstance(expires_at, float) and not math.isfinite(expires_at):
        raise ClaudeDesktopAuthError(_CLAUDE_CREDENTIALS_INVALID_MESSAGE, kind="auth")
    minimum_expiry = (time.time() + CLAUDE_DESKTOP_AUTH_TTL_SECONDS) * 1000
    if expires_at <= minimum_expiry:
        raise ClaudeDesktopAuthError(
            "Claude subscription authentication expired. Start a new Cowork task to refresh it.",
            kind="auth",
        )
    return token


def _run_claude_auth_command(
    claude_binary: str,
    arguments: list[str],
    *,
    timeout: float,
    show_stdout: bool,
) -> tuple[str, subprocess.CompletedProcess]:
    def run(binary: str) -> subprocess.CompletedProcess:
        return subprocess_cross_os.run(
            [binary, "auth", *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr if show_stdout else subprocess.DEVNULL,
            stderr=sys.stderr,
            timeout=timeout,
        )

    try:
        return claude_binary, run(claude_binary)
    except OSError:
        fallback = shutil.which("claude")
        if not fallback:
            raise
        fallback = os.path.abspath(fallback)
        if os.path.normcase(fallback) == os.path.normcase(os.path.abspath(claude_binary)):
            raise
        return fallback, run(fallback)


def _run_claude_status(claude_binary: str, *, timeout: float) -> str:
    try:
        claude_binary, _ = _run_claude_auth_command(
            claude_binary,
            ["status", "--json"],
            timeout=timeout,
            show_stdout=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeDesktopAuthError(
            "Claude Code could not refresh subscription authentication. "
            "Install or repair Claude Code, or run `ug configure`, then start a new Cowork task.",
            kind="cli",
        ) from exc
    return claude_binary


def _run_claude_login(claude_binary: str, *, timeout: float) -> None:
    try:
        _, result = _run_claude_auth_command(
            claude_binary,
            ["login", "--claudeai"],
            timeout=timeout,
            show_stdout=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeDesktopAuthError(
            "Claude subscription sign-in failed. Run `claude auth login`, then start a new Cowork task.",
            kind="auth",
        ) from exc
    if result.returncode != 0:
        raise ClaudeDesktopAuthError(
            "Claude subscription sign-in failed. Run `claude auth login`, then start a new Cowork task.",
            kind="auth",
        )


def _try_claude_auth_token() -> str | ClaudeDesktopAuthError:
    try:
        return _claude_subscription_token()
    except ClaudeDesktopAuthError as exc:
        # Only authentication failures can be fixed by refreshing or signing in.
        if exc.kind != "auth":
            raise
        return exc


def _get_claude_auth_token(claude_binary: str, *, deadline: float | None = None) -> str:
    """Return a valid Claude subscription token, refreshing or signing in when needed."""
    if deadline is None:
        deadline = time.monotonic() + _HELPER_BUDGET_SECONDS

    # Avoid spawning Claude while its cached token is still valid.
    token = _try_claude_auth_token()
    if isinstance(token, str):
        return token

    with _auth_lock(deadline):
        # Another helper may have refreshed the token while this process waited for the lock.
        token = _try_claude_auth_token()
        if isinstance(token, str):
            return token

        claude_binary = _run_claude_status(
            claude_binary,
            timeout=_remaining_budget(deadline, _TOKEN_TIMEOUT_SECONDS),
        )
        # `claude auth status` can silently renew an expired token in the shared credential store.
        token = _try_claude_auth_token()
        if isinstance(token, str):
            return token
        if not _interactive_context():
            raise token

        _run_claude_login(
            claude_binary,
            timeout=_remaining_budget(deadline, CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS),
        )
        return _claude_subscription_token()


def get_claude_desktop_relay_credential(
    host: str,
    profile: str,
    claude_binary: str,
) -> dict[str, object]:
    """Return both credentials required by an Anthropic subscription relay."""
    deadline = time.monotonic() + _HELPER_BUDGET_SECONDS
    databricks_token = get_claude_desktop_databricks_token(host, profile)
    return {
        "token": _get_claude_auth_token(claude_binary, deadline=deadline),
        "headers": {AI_GATEWAY_TOKEN_HEADER: f"Bearer {databricks_token}"},
    }


__all__ = [
    "CLAUDE_DESKTOP_AUTH_SILENT_REFRESH",
    "CLAUDE_DESKTOP_AUTH_TIMEOUT_MS",
    "CLAUDE_DESKTOP_AUTH_TIMEOUT_SECONDS",
    "CLAUDE_DESKTOP_AUTH_TTL_MS",
    "CLAUDE_DESKTOP_AUTH_TTL_SECONDS",
    "CLAUDE_HELPER_CONTEXT_ENV",
    "CLAUDE_HELPER_INTERACTIVE_CONTEXT",
    "CLAUDE_CODE_OAUTH_TOKEN_ENV",
    "CLAUDE_CONFIG_DIR_ENV",
    "ClaudeDesktopAuthError",
    "get_claude_desktop_relay_credential",
    "get_claude_desktop_databricks_token",
]
