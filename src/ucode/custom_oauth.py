"""Custom-client OAuth, separate from production Databricks CLI auth."""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from databricks.sdk import oauth

from ucode.constants import LOCALHOST, LOOPBACK_HOST
from ucode.databricks import (
    build_auth_token_argv,
    databricks_cli_version,
    run,
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


def _get_custom_client_token_from_cli(
    workspace: str,
    client_id: str,
    *,
    profile: str | None = None,
    force_refresh: bool = False,
) -> str:
    version = databricks_cli_version()
    if version is None or version < CUSTOM_OAUTH_CLI_MIN_VERSION:
        current = "an unreadable version" if version is None else ".".join(map(str, version))
        required = ".".join(map(str, CUSTOM_OAUTH_CLI_MIN_VERSION))
        raise RuntimeError(
            "Custom-client OAuth via Databricks CLI requires Databricks CLI "
            f"v{required} or newer; found {current}. Install or upgrade the CLI, then retry."
        )
    env = os.environ.copy()
    env["DATABRICKS_CLIENT_ID"] = client_id
    args = ["databricks", "auth", "token"]
    args.extend(["--host", workspace])
    if profile:
        args.extend(["--profile", profile])
    if force_refresh:
        args.append("--force-refresh")
    try:
        result = run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=CUSTOM_OAUTH_TIMEOUT_MS // 1000,
        )
        token = json.loads(result.stdout or "{}").get("access_token", "")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise RuntimeError("Custom-client OAuth via Databricks CLI failed.") from exc
    if result.returncode != 0 or not token:
        raise RuntimeError(
            "Custom-client OAuth via Databricks CLI failed. Run "
            f"`databricks auth login --host {workspace} --client-id {client_id}` and retry."
        )
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
    """Reuse the SDK's PKCE flow and per-workspace/client token cache."""
    workspace = normalize_workspace_url(workspace)
    if os.environ.get("ENABLE_CUSTOM_OAUTH_FROM_CLI") == "1":
        return _get_custom_client_token_from_cli(
            workspace,
            client_id,
            profile=profile,
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
