"""Smart-routing session isolation and exclusive workspace ownership."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != Path(__file__).parent:
        raise pytest.UsageError("Run with --confcutdir=tests/e2e_cuj to exclude unit-test mocks.")


@pytest.fixture
def cuj(request, setup_workspace, tmp_path):
    from helpers.session import MANAGED_PATHS, UserSession
    from helpers.workspace import Workspace

    assert os.name == "posix", "Full TUI CUJs require a disposable POSIX runner"
    assert not any(path.exists() for path in MANAGED_PATHS), (
        "Existing machine-wide agent settings; use a clean disposable runner. Nothing was changed."
    )
    for tool in ("ug", "claude", "codex", "databricks"):
        assert shutil.which(tool), f"Install the required CLI before running this CUJ: {tool}"

    authorization = request.instance.workspace.config.authenticate().get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization.removeprefix("Bearer "):
        raise RuntimeError("Service-principal authentication did not return a bearer token.")
    bearer = authorization.removeprefix("Bearer ")
    workspace_url = request.instance.WORKSPACE_URL.rstrip("/")
    artifacts = tmp_path / "artifacts"
    print(f"CUJ artifacts: {artifacts}")
    with tempfile.TemporaryDirectory(prefix="ug-cuj-", dir="/tmp") as temporary:
        session = UserSession(Path(temporary), Path(shutil.which("ug")), artifacts, bearer)
        workspace = Workspace(workspace_url, bearer, session.record)
        with workspace.reserved():
            try:
                yield session, workspace
            finally:
                try:
                    session.cleanup()
                except BaseException:
                    workspace.quarantined = True
                    raise
