"""Isolated local sessions using read-only workspace configuration."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from databricks.sdk.errors import DatabricksError

from tests.integration.utils.terminal import TerminalProcess

from .helpers.constants import CLAUDE, CODEX, MANAGED_PATHS
from .helpers.session import UserSession
from .helpers.tui_request_recorder import TuiRequestRecorder
from .helpers.workspace import Workspace


def pytest_configure(config):
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != Path(__file__).parent:
        raise pytest.UsageError("Run with --confcutdir=tests/e2e_cuj to exclude unit-test mocks.")


@pytest.fixture(scope="class")
def cuj(request, setup_workspace, tmp_path_factory):
    assert os.name == "posix", "Full TUI CUJs require a disposable POSIX runner"
    assert not any(path.exists() for path in MANAGED_PATHS), (
        "Existing machine-wide agent settings; use a clean disposable runner. Nothing was changed."
    )
    for tool in ("ug", CLAUDE, CODEX, "databricks"):
        assert shutil.which(tool), f"Install the required CLI before running this CUJ: {tool}"

    authorization = request.cls.workspace.config.authenticate().get("Authorization", "")
    if not authorization.startswith("Bearer ") or not authorization.removeprefix("Bearer "):
        raise RuntimeError("Service-principal authentication did not return a bearer token.")
    bearer = authorization.removeprefix("Bearer ")
    run_directory = tmp_path_factory.mktemp(request.cls.__name__)
    artifacts = run_directory / "artifacts"
    print(f"CUJ artifacts: {artifacts}")
    with tempfile.TemporaryDirectory(prefix="ug-cuj-", dir="/tmp") as temporary:
        workspace = Workspace(request.cls.workspace)
        with TuiRequestRecorder(workspace.url) as recorder:
            session = UserSession(Path(temporary), Path(shutil.which("ug")), artifacts, bearer)
            try:
                published = workspace.config()
                try:
                    yield session, workspace, recorder
                finally:
                    try:
                        workspace.assert_unchanged(published)
                    finally:
                        state_dir = session.home / ".ucode"
                        if any(
                            (state_dir / name).is_file()
                            for name in ("state.json", "managed-backups/manifest.json")
                        ):
                            with TerminalProcess(
                                session,
                                "ug",
                                [str(session.binary), "revert"],
                                "cleanup-revert",
                            ) as terminal:
                                terminal.finish()
                        assert not any(path.exists() for path in MANAGED_PATHS), (
                            "CUJ teardown left machine-wide agent settings"
                        )
            except DatabricksError as error:
                # Server messages may echo credentials; retain only the SDK error type.
                raise RuntimeError(f"Workspace API failed: {type(error).__name__}") from None
