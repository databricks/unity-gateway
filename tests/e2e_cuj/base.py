"""Base class for CUJs with a dedicated workspace and service-principal auth."""

import os
from typing import ClassVar

import pytest
from databricks.sdk import WorkspaceClient


def make_workspace_client(url: str, client_id_env: str, client_secret_env: str) -> WorkspaceClient:
    client_id = os.environ.get(client_id_env, "")
    client_secret = os.environ.get(client_secret_env, "")
    if not client_id or not client_secret:
        pytest.fail(f"Set {client_id_env} and {client_secret_env}.", pytrace=False)
    return WorkspaceClient(
        host=url,
        client_id=client_id,
        client_secret=client_secret,
        auth_type="oauth-m2m",
    )


def bearer(client: WorkspaceClient) -> str:
    """Mint a short-lived bearer; only it, never the SP secret, reaches agent processes."""
    authorization = client.config.authenticate().get("Authorization", "")
    token = authorization.removeprefix("Bearer ")
    if not authorization.startswith("Bearer ") or not token:
        raise RuntimeError("Service-principal authentication did not return a bearer token.")
    return token


class BaseCujTest:
    WORKSPACE_URL: ClassVar[str] = ""
    CLIENT_ID_ENV: ClassVar[str] = "UG_CUJ_SP_CLIENT_ID"
    CLIENT_SECRET_ENV: ClassVar[str] = "UG_CUJ_SP_CLIENT_SECRET"
    workspace: WorkspaceClient

    @pytest.fixture(scope="class", autouse=True)
    def setup_workspace(self, request):
        workspace_url = request.cls.WORKSPACE_URL
        if not workspace_url:
            pytest.fail("Set WORKSPACE_URL on your CUJ test class.", pytrace=False)

        request.cls.workspace = make_workspace_client(
            workspace_url, request.cls.CLIENT_ID_ENV, request.cls.CLIENT_SECRET_ENV
        )
