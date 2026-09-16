"""Custom-client OAuth, separate from production Databricks CLI auth."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import subprocess
from collections.abc import Sequence
from typing import TypedDict
from urllib.parse import urlparse

from ucode.config_io import APP_DIR
from ucode.constants import LOCALHOST, LOOPBACK_HOST
from ucode.databricks import build_auth_token_argv
from ucode.ui import normalize_workspace_url

DEFAULT_REDIRECT_URL = f"http://{LOCALHOST}:8020"
# Custom OAuth may need a human to finish browser consent, not just a token fetch.
CUSTOM_OAUTH_TIMEOUT_MS = 180_000
CUSTOM_OAUTH_CLI_VERSION = (1, 17, 0)
CUSTOM_OAUTH_CONFIG_FILE = APP_DIR / "custom-oauth.databrickscfg"


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


def build_custom_auth_token_argv(workspace: str, config: CustomOAuthConfig) -> list[str]:
    normalized = create_custom_oauth_config(
        config["client_id"], config["scopes"], config["redirect_url"]
    )
    return [
        *build_auth_token_argv(workspace),
        "--client-id",
        normalized["client_id"],
        "--redirect-url",
        normalized["redirect_url"],
        "--scopes",
        ",".join(normalized["scopes"]),
    ]


def build_custom_auth_shell_command(workspace: str, config: CustomOAuthConfig) -> str:
    argv = build_custom_auth_token_argv(workspace, config)
    if platform.system() == "Windows":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _custom_oauth_profile(workspace: str, client_id: str, scopes: Sequence[str]) -> str:
    key = "\0".join((workspace, client_id, *scopes)).encode()
    return f"ug-custom-oauth-{hashlib.sha256(key).hexdigest()[:12]}"


def _require_custom_oauth_cli() -> None:
    from ucode.databricks import databricks_cli_version

    version = databricks_cli_version()
    if version is None or version < CUSTOM_OAUTH_CLI_VERSION:
        required = ".".join(map(str, CUSTOM_OAUTH_CLI_VERSION))
        raise RuntimeError(
            f"Custom-client OAuth requires Databricks CLI v{required} or newer. Upgrade the CLI "
            "and retry."
        )


def _token_from_cli(workspace: str, profile: str, env: dict[str, str], force: bool) -> str:
    command = [
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
    if force:
        command.append("--force-refresh")
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
    if result.returncode != 0:
        return ""
    try:
        return json.loads(result.stdout or "{}").get("access_token", "")
    except json.JSONDecodeError:
        return ""


def get_custom_client_token(
    workspace: str,
    client_id: str,
    redirect_url: str = DEFAULT_REDIRECT_URL,
    *,
    scopes: Sequence[str],
    force_refresh: bool = False,
) -> str:
    """Delegate custom-client U2M login, refresh, and caching to Databricks CLI."""
    config = create_custom_oauth_config(client_id, scopes, redirect_url)
    workspace = normalize_workspace_url(workspace)
    _require_custom_oauth_cli()
    profile = _custom_oauth_profile(workspace, config["client_id"], config["scopes"])
    CUSTOM_OAUTH_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DATABRICKS_CONFIG_FILE"] = str(CUSTOM_OAUTH_CONFIG_FILE)

    try:
        token = _token_from_cli(workspace, profile, env, force_refresh)
        if token:
            return token
        login = subprocess.run(
            [
                "databricks",
                "auth",
                "login",
                "--host",
                workspace,
                "--profile",
                profile,
                "--client-id",
                config["client_id"],
                "--scopes",
                ",".join(config["scopes"]),
                "--timeout",
                "3m",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=CUSTOM_OAUTH_TIMEOUT_MS / 1000,
        )
        if login.returncode == 0:
            token = _token_from_cli(workspace, profile, env, False)
        if token:
            return token
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Databricks CLI custom-client OAuth failed. Check the workspace, client ID, and "
            "scopes, then retry."
        ) from exc
    raise RuntimeError(
        "Databricks CLI returned no custom-client OAuth token. Check the workspace, client ID, "
        "and scopes, then retry."
    )
