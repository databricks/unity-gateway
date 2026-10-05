"""Standalone fixtures for dedicated-workspace CUJs."""

from __future__ import annotations

import shutil

import pytest

from tests.integration.conftest import installed_binary as installed_binary
from tests.integration.conftest import session as session


@pytest.fixture
def live_session(session, setup_workspace, request):
    """Reuse the installed-product session with a fresh workspace bearer."""
    headers = request.instance.workspace.config.authenticate()
    session.env["DATABRICKS_BEARER"] = headers["Authorization"].removeprefix("Bearer ")
    for binary in ("databricks", "claude", "codex"):
        if not shutil.which(binary, path=session.env["PATH"]):
            pytest.fail(f"Required CUJ binary is missing: {binary}", pytrace=False)
    return session
