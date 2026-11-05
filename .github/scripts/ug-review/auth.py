#!/usr/bin/env python3
"""Reuse the UG review service principal to authenticate the official Claude action."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class GatewayAuthError(RuntimeError):
    """The review job cannot obtain a gateway credential."""


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GatewayAuthError(f"Required environment variable {name} is missing.")
    return value


def _oauth_access_token(host: str) -> str:
    client_id = _env("DATABRICKS_CLIENT_ID")
    client_secret = _env("DATABRICKS_CLIENT_SECRET")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        f"{host.rstrip('/')}/oidc/v1/token",
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "all-apis"}
        ).encode(),
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GatewayAuthError(
            f"Databricks OAuth token request failed (HTTP {exc.code}). "
            "Check UG_CLI_REVIEW_BOT_SP_CLIENT_SECRET and service-principal access."
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GatewayAuthError(f"Databricks OAuth token request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GatewayAuthError("Databricks OAuth token request returned invalid JSON.") from exc
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or any(character.isspace() for character in token):
        raise GatewayAuthError("Databricks OAuth token request returned no valid access token.")
    return token


def main() -> int:
    try:
        output_path = Path(_env("GITHUB_OUTPUT"))
        token = _oauth_access_token(_env("DATABRICKS_HOST"))
        print(f"::add-mask::{token}", flush=True)
        with output_path.open("a", encoding="utf-8") as output:
            output.write(f"token={token}\n")
        return 0
    except (GatewayAuthError, OSError) as exc:
        print(f"::error::{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
