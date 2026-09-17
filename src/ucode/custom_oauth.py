"""Custom-client OAuth via the SDK cache or dedicated Databricks CLI profiles."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from databricks.sdk import oauth

from ucode import config_io
from ucode.constants import LOCALHOST, LOOPBACK_HOST
from ucode.databricks import (
    build_auth_token_argv,
    build_databricks_cli_env,
    databricks_cli_version,
    read_databricks_oauth_profile,
    run,
    run_databricks_login,
)
from ucode.ui import err_console, normalize_workspace_url, print_warning_err

DEFAULT_REDIRECT_URL = f"http://{LOCALHOST}:8020"
# Custom OAuth may need a human to finish browser consent, not just a token fetch.
CUSTOM_OAUTH_TIMEOUT_MS = 180_000
CUSTOM_OAUTH_CLI_MIN_VERSION = (1, 17, 0)


class CustomOAuthConfig(TypedDict):
    client_id: str
    redirect_url: str
    scopes: list[str]


def _normalize_scopes(scopes: Sequence[str]) -> list[str]:
    if isinstance(scopes, str):
        raise RuntimeError("OAuth scopes must be provided as a sequence of scope names.")
    requested_scopes = list(dict.fromkeys(scope.strip() for scope in scopes if scope.strip()))
    if "offline_access" not in requested_scopes:
        raise RuntimeError("OAuth scopes must include offline_access to support token refresh.")
    if not any(scope != "offline_access" for scope in requested_scopes):
        raise RuntimeError("At least one API OAuth scope must be provided.")
    return requested_scopes


def _validate_redirect_url(redirect_url: str) -> None:
    try:
        redirect = urlparse(redirect_url)
        valid_redirect = (
            redirect.scheme == "http"
            and redirect.hostname in {LOCALHOST, LOOPBACK_HOST}
            and redirect.port is not None
            and redirect.port > 0
            and not (redirect.username or redirect.password or redirect.query or redirect.fragment)
        )
    except ValueError:
        valid_redirect = False
    if not valid_redirect:
        raise RuntimeError("--redirect-url must be a local HTTP callback with a port.")


def create_custom_oauth_config(
    client_id: str,
    scopes: Sequence[str],
    redirect_url: str = DEFAULT_REDIRECT_URL,
) -> CustomOAuthConfig:
    client_id = client_id.strip()
    if not client_id:
        raise RuntimeError("--client-id must not be empty.")
    _validate_redirect_url(redirect_url)
    return {
        "client_id": client_id,
        "redirect_url": redirect_url,
        "scopes": _normalize_scopes(scopes),
    }


def build_custom_auth_token_argv(
    workspace: str,
    config: CustomOAuthConfig,
    profile: str | None = None,
) -> list[str]:
    normalized = create_custom_oauth_config(
        config["client_id"], config["scopes"], config["redirect_url"]
    )
    return [
        *build_auth_token_argv(workspace, profile),
        "--client-id",
        normalized["client_id"],
        "--redirect-url",
        normalized["redirect_url"],
        "--scopes",
        ",".join(normalized["scopes"]),
    ]


def build_custom_auth_shell_command(
    workspace: str,
    config: CustomOAuthConfig,
    profile: str | None = None,
) -> str:
    argv = build_custom_auth_token_argv(workspace, config, profile)
    if platform.system() == "Windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


@contextmanager
def _custom_oauth_lock(cache_dir: Path, redirect_url: str) -> Iterator[None]:
    """Serialize helpers sharing a callback port with a POSIX file lock.

    Keep the lock file in place: unlinking it could let waiters lock different
    inodes. The OS releases the lock even if the helper is killed on timeout.
    """
    import fcntl

    cache_dir.mkdir(parents=True, exist_ok=True)
    port = urlparse(redirect_url).port
    with (cache_dir / f"ug-oauth-{port}.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _trace_custom_oauth_cli(message: str) -> None:
    log_path = config_io.APP_DIR / "custom-oauth-cli.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now(UTC).isoformat()} pid={os.getpid()} {message}\n")
    except OSError as exc:
        print_warning_err(f"Could not write custom OAuth CLI trace: {exc}")


def _require_custom_oauth_cli() -> None:
    version = databricks_cli_version()
    if version is None or version < CUSTOM_OAUTH_CLI_MIN_VERSION:
        current = "an unreadable version" if version is None else ".".join(map(str, version))
        required = ".".join(map(str, CUSTOM_OAUTH_CLI_MIN_VERSION))
        raise RuntimeError(
            "Custom-client OAuth via Databricks CLI requires Databricks CLI "
            f"v{required} or newer; found {current}. Install or upgrade the CLI, then retry."
        )


def _profile_matches_client(fields: dict[str, str] | None, workspace: str, client_id: str) -> bool:
    return bool(
        fields
        and fields["host"].rstrip("/") == workspace
        and fields["client_id"] == client_id
        and fields["auth_type"] == "databricks-cli"
    )


def _custom_cli_profile(workspace: str, client_id: str, profile: str | None) -> str:
    """Reuse an explicitly selected custom-client profile, or derive an isolated one."""
    if profile and _profile_matches_client(
        read_databricks_oauth_profile(profile), workspace, client_id
    ):
        return profile
    key = hashlib.sha256(f"{workspace}\0{client_id}".encode()).hexdigest()[:16]
    label = re.sub(r"[^a-zA-Z0-9_-]", "-", client_id)[:36]
    dedicated = f"ug-oauth-{label}-{key}"
    fields = read_databricks_oauth_profile(dedicated)
    if fields is not None and not _profile_matches_client(fields, workspace, client_id):
        raise RuntimeError(
            f"Databricks profile '{dedicated}' exists with different authentication settings. "
            "Rename that profile before retrying; UG will not overwrite it."
        )
    return dedicated


def _profile_has_scopes(fields: dict[str, str], scopes: Sequence[str]) -> bool:
    # The CLI adds offline_access automatically, even when it isn't saved in the profile.
    saved = {scope.strip() for scope in fields["scopes"].split(",") if scope.strip()}
    return (set(scopes) - {"offline_access"}).issubset(saved or {"all-apis"})


def ensure_custom_oauth_cli_profile(
    workspace: str,
    config: CustomOAuthConfig,
    profile: str | None = None,
    *,
    force_login: bool = False,
) -> str:
    """Authenticate a workspace/client-specific CLI profile before the agent starts."""
    _require_custom_oauth_cli()
    workspace = normalize_workspace_url(workspace)
    config = create_custom_oauth_config(
        config["client_id"], config["scopes"], config["redirect_url"]
    )
    profile = _custom_cli_profile(workspace, config["client_id"], profile)
    fields = read_databricks_oauth_profile(profile)
    if fields is not None and _profile_has_scopes(fields, config["scopes"]) and not force_login:
        try:
            _get_custom_client_token_from_cli(
                workspace, config["client_id"], profile=profile, scopes=config["scopes"]
            )
            return profile
        except RuntimeError:
            pass  # Expired/revoked credentials: reauthenticate while the terminal is available.
    if config["redirect_url"] != DEFAULT_REDIRECT_URL:
        print_warning_err(
            "Databricks CLI does not support --redirect-url; its callback is "
            f"{DEFAULT_REDIRECT_URL} (or a higher port if busy). Register the CLI callback "
            "on your OAuth application before signing in. The supplied redirect URL "
            "is only used by the SDK path."
        )
    run_databricks_login(
        workspace,
        profile,
        client_id=config["client_id"],
        scopes=[scope for scope in config["scopes"] if scope != "offline_access"],
    )
    # Verify both the saved client_id and its token; never fall back to workspace-only auth.
    _get_custom_client_token_from_cli(
        workspace, config["client_id"], profile=profile, scopes=config["scopes"]
    )
    return profile


@contextmanager
def custom_oauth_cli_environment(
    workspace: str, config: CustomOAuthConfig, profile: str
) -> Iterator[None]:
    """Pin generic token consumers and their children to this session's custom helper."""
    values = {
        "DATABRICKS_BEARER": None,
        "DATABRICKS_BEARER_COMMAND": build_custom_auth_shell_command(workspace, config, profile),
        "DATABRICKS_CONFIG_PROFILE": profile,
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _get_custom_client_token_from_cli(
    workspace: str,
    client_id: str,
    *,
    profile: str | None = None,
    scopes: Sequence[str] | None = None,
    force_refresh: bool = False,
) -> str:
    _require_custom_oauth_cli()
    profile = _custom_cli_profile(workspace, client_id, profile)
    login_args = [
        "databricks",
        "auth",
        "login",
        "--host",
        workspace,
        "--profile",
        profile,
        "--client-id",
        client_id,
    ]
    if scopes is not None:
        login_args.extend(["--scopes", ",".join(s for s in scopes if s != "offline_access")])
    hint = f"Run `{shlex.join(login_args)}` or relaunch UG with --client-id to sign in."
    fields = read_databricks_oauth_profile(profile)
    if not _profile_matches_client(fields, workspace, client_id):
        raise RuntimeError(f"Custom OAuth profile '{profile}' is not configured. {hint}")
    if scopes is not None and fields is not None and not _profile_has_scopes(fields, scopes):
        raise RuntimeError(f"Custom OAuth profile '{profile}' needs the requested scopes. {hint}")
    env = build_databricks_cli_env(workspace, profile)
    # auth token reads client_id from the named profile, not this environment variable.
    env.pop("DATABRICKS_CLIENT_ID", None)
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    args = [
        "databricks",
        "auth",
        "token",
        "--host",
        workspace,
        "--profile",
        profile,
        "--output",
        "json",
    ]
    if force_refresh:
        args.append("--force-refresh")
    err_console.print(
        f"Fetching custom OAuth token via Databricks CLI (profile: {profile}, client ID: {client_id})...",
        markup=False,
    )
    _trace_custom_oauth_cli(f"Running {shlex.join(args)}")
    try:
        result = run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=CUSTOM_OAUTH_TIMEOUT_MS // 1000,
        )
        payload = json.loads(result.stdout or "{}")
        token = payload.get("access_token", "") if isinstance(payload, dict) else ""
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        _trace_custom_oauth_cli(f"CLI token fetch failed: {type(exc).__name__}")
        raise RuntimeError(f"Custom-client OAuth via Databricks CLI failed. {hint}") from exc
    if result.returncode != 0 or not isinstance(token, str) or not token.strip():
        _trace_custom_oauth_cli(f"CLI token fetch failed (exit code {result.returncode}).")
        raise RuntimeError(f"Custom-client OAuth via Databricks CLI failed. {hint}")
    _trace_custom_oauth_cli("CLI token fetch succeeded.")
    return token


def get_custom_client_token(
    workspace: str,
    client_id: str,
    redirect_url: str = DEFAULT_REDIRECT_URL,
    *,
    scopes: Sequence[str] | None,
    profile: str | None = None,
    force_refresh: bool = False,
) -> str:
    """Fetch a custom-client token through the selected SDK or CLI backend."""
    workspace = normalize_workspace_url(workspace)
    client_id = client_id.strip()
    if not client_id:
        raise RuntimeError("--client-id must not be empty.")
    if os.environ.get("ENABLE_CUSTOM_OAUTH_FROM_CLI") == "1":
        return _get_custom_client_token_from_cli(
            workspace,
            client_id,
            profile=profile,
            scopes=_normalize_scopes(scopes) if scopes is not None else None,
            force_refresh=force_refresh,
        )
    if scopes is None:
        raise RuntimeError("OAuth scopes are required for custom-client OAuth.")
    config = create_custom_oauth_config(client_id, scopes, redirect_url)
    try:
        endpoints = oauth.get_workspace_endpoints(workspace)
        cache = oauth.TokenCache(
            host=workspace,
            oidc_endpoints=endpoints,
            client_id=config["client_id"],
            redirect_url=config["redirect_url"],
            scopes=config["scopes"],
        )
        with _custom_oauth_lock(Path(cache.filename).parent, config["redirect_url"]):
            # Read only after acquiring the lock: another helper may have just
            # completed login or rotated the refresh token while we waited.
            credentials = cache.load()
            if credentials is not None:
                try:
                    if force_refresh:
                        credentials = oauth.SessionCredentials(
                            token=credentials.refresh(),
                            token_endpoint=endpoints.token_endpoint,
                            client_id=config["client_id"],
                            redirect_url=config["redirect_url"],
                        )
                    credentials.token()
                except Exception:
                    print_warning_err("Cached OAuth token could not be refreshed. Sign in again.")
                    credentials = None
            if credentials is None:
                client = oauth.OAuthClient(
                    oidc_endpoints=endpoints,
                    client_id=config["client_id"],
                    redirect_url=config["redirect_url"],
                    scopes=config["scopes"],
                )
                consent = client.initiate_consent()
                err_console.print(
                    f"Sign in using your browser: {consent.authorization_url}",
                    markup=False,
                    soft_wrap=True,
                )
                credentials = consent.launch_external_browser()
            token = credentials.token().access_token
            if not token:
                raise ValueError("OAuth returned no access token")
            cache.save(credentials)
            return token
    except Exception as exc:
        raise RuntimeError(
            "Custom-client OAuth failed. Check the workspace, client ID, and registered "
            f"redirect URL ({config['redirect_url']}); ensure its local port is available and "
            "the SDK token cache is writable, then retry."
        ) from exc
