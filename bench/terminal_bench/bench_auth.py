"""Workspace auth for bench runs.

With service-principal credentials set, mint a fresh bearer per task on the host so
long runs outlive a single token and the SP secret never reaches the agent.
Otherwise fall back to a pre-fetched DATABRICKS_BEARER.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request

SECRET_ENV = ("UG_BENCH_CLIENT_ID", "UG_BENCH_CLIENT_SECRET")


def _sp_credentials() -> tuple[str, str] | None:
    client_id, secret = (os.environ.get(name, "").strip() for name in SECRET_ENV)
    return (client_id, secret) if client_id and secret else None


def workspace() -> str:
    if _sp_credentials() and (host := os.environ.get("UG_BENCH_SP_WORKSPACE", "").strip()):
        return host
    host = os.environ.get("UCODE_TEST_WORKSPACE", "").strip()
    if not host:
        raise RuntimeError("Set UCODE_TEST_WORKSPACE, or UG_BENCH_SP_WORKSPACE with SP credentials")
    return host


def bearer(host: str) -> str:
    credentials = _sp_credentials()
    if credentials is None:
        token = os.environ.get("DATABRICKS_BEARER", "").strip()
        if not token:
            raise RuntimeError(
                "Set DATABRICKS_BEARER, or UG_BENCH_CLIENT_ID/UG_BENCH_CLIENT_SECRET"
            )
        return token
    basic = base64.b64encode(":".join(credentials).encode()).decode()
    request = urllib.request.Request(
        f"{host.rstrip('/')}/oidc/v1/token",
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "all-apis"}
        ).encode(),
        headers={"Authorization": f"Basic {basic}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["access_token"]


def agent_env(base: dict[str, str]) -> dict[str, str]:
    """Copy of `base` with a fresh bearer and without the SP secret."""
    env = {key: value for key, value in base.items() if key not in SECRET_ENV}
    host = workspace()
    env["DATABRICKS_HOST"] = host
    env["DATABRICKS_BEARER"] = bearer(host)
    return env
