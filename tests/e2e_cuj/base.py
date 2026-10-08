"""Base class for CUJs with a dedicated workspace and service-principal auth."""

import os
from typing import ClassVar

import pytest
from databricks.sdk import WorkspaceClient


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

        client_id_env = request.cls.CLIENT_ID_ENV
        client_secret_env = request.cls.CLIENT_SECRET_ENV
        client_id = os.environ.get(client_id_env, "")
        client_secret = os.environ.get(client_secret_env, "")
        if not client_id or not client_secret:
            pytest.fail(f"Set {client_id_env} and {client_secret_env}.", pytrace=False)

        request.cls.workspace = WorkspaceClient(
            host=workspace_url,
            client_id=client_id,
            client_secret=client_secret,
            auth_type="oauth-m2m",
        )
