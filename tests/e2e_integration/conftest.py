"""Independent collection and per-CUJ live resource ownership."""

from pathlib import Path

import pytest
from helpers.base import BaseCujTest


def pytest_configure(config):
    suite = Path(__file__).resolve().parent
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != suite:
        raise pytest.UsageError(
            "Full E2E CUJs require --confcutdir=tests/e2e_integration. "
            "Use scripts/run_integration.py --suite e2e-integration for live runs."
        )


def pytest_collection_modifyitems(items):
    workspaces = {}
    for item in items:
        if item.cls is None or not issubclass(item.cls, BaseCujTest):
            raise pytest.UsageError(f"{item.nodeid} must belong to a BaseCujTest subclass.")
        try:
            workspace = item.cls.validated_workspace_url()
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None
        if workspace in workspaces:
            raise pytest.UsageError(
                f"{item.nodeid} and {workspaces[workspace]} share a Databricks workspace. "
                "Assign a separate WORKSPACE_URL to each test."
            )
        workspaces[workspace] = item.nodeid


@pytest.fixture
def cuj(request):
    import os
    import shutil
    import tempfile
    from uuid import uuid4

    from helpers.session import MANAGED_PATHS, UserSession
    from helpers.workspace import Workspace

    workspace_url = request.instance.workspace_url
    credential_url = os.environ.get("UCODE_TEST_WORKSPACE", "").rstrip("/").lower()
    assert credential_url == workspace_url, (
        "Runner --workspace must match this CUJ's WORKSPACE_URL; no global workspace fallback"
    )
    bearer = os.environ.get("DATABRICKS_BEARER", "")
    binary = Path(os.environ.get("UG_INTEGRATION_BIN", ""))
    run_dir = os.environ.get("UG_INTEGRATION_RUN_DIR")
    # Never put a nonempty credential in a pytest-rewritten assert expression.
    if not bearer:
        raise AssertionError("Supply an explicit bearer/profile through the integration runner")
    assert binary.is_file() and run_dir, (
        "Use scripts/run_integration.py with explicit auth and versions"
    )
    assert os.name == "posix", "Full TUI CUJs require a disposable POSIX runner"
    assert not any(path.exists() for path in MANAGED_PATHS), (
        "Existing machine-wide agent settings; use a clean disposable runner. Nothing was changed."
    )
    required = {mark.name for mark in request.node.iter_markers()} & {"claude", "codex"}
    assert required <= set(os.environ.get("UG_INTEGRATION_AGENTS", "").split(",")), (
        f"This CUJ requires exact versions for all agents: {sorted(required)}"
    )
    for tool in sorted(required | {"databricks"}):
        assert shutil.which(tool), f"Runner did not install required binary: {tool}"
    artifacts = Path(run_dir) / "artifacts" / f"{request.node.name}-{uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory(prefix="ug-cuj-", dir="/tmp") as temporary:
        session = UserSession(Path(temporary), binary, artifacts, bearer)
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
