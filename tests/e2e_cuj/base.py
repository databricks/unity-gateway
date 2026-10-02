"""Base class for CUJs with a dedicated workspace and service-principal auth."""

import os
from typing import ClassVar

import pytest
from databricks.sdk import WorkspaceClient


class BaseCujTest:
    WORKSPACE_URL: ClassVar[str] = ""
    workspace: WorkspaceClient

    @pytest.fixture(autouse=True)
    def setup_workspace(self):
        if not self.WORKSPACE_URL:
            pytest.fail("Set WORKSPACE_URL on your CUJ test class.", pytrace=False)

        client_id = os.environ.get("UG_CUJ_SP_CLIENT_ID", "")
        client_secret = os.environ.get("UG_CUJ_SP_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            pytest.fail("Set UG_CUJ_SP_CLIENT_ID and UG_CUJ_SP_CLIENT_SECRET.", pytrace=False)

        self.workspace = WorkspaceClient(
            host=self.WORKSPACE_URL,
            client_id=client_id,
            client_secret=client_secret,
            auth_type="oauth-m2m",
        )
