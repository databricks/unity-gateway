"""Isolated local sessions using read-only workspace configuration."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from databricks.sdk.errors import DatabricksError

from .base import bearer
from .helpers.constants import CLAUDE, CODEX, MANAGED_PATHS, WORKSPACE_POLICY_PATH
from .helpers.session import (
    MACHINE_WIDE_LEAK,
    UserSession,
    dirty_runner_message,
    record_machine_wide_leak,
)
from .helpers.tui_request_recorder import TuiRequestRecorder
from .helpers.workspace import Workspace


def pytest_configure(config):
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != Path(__file__).parent:
        raise pytest.UsageError("Run with --confcutdir=tests/e2e_cuj to exclude unit-test mocks.")


def _scrubbed(error: DatabricksError) -> RuntimeError:
    # Server messages may echo credentials; retain only the SDK error type.
    return RuntimeError(f"Workspace API failed: {type(error).__name__}")


@pytest.fixture(scope="class")
def cuj(request, setup_workspace, tmp_path_factory):
    assert os.name == "posix", "Full TUI CUJs require a disposable POSIX runner"
    # Teardown removes the workspace policy, so never start on one this run didn't install.
    assert not WORKSPACE_POLICY_PATH.exists(), (
        f"Existing workspace policy {WORKSPACE_POLICY_PATH}; use a clean disposable runner. "
        "Nothing was changed."
    )
    assert not any(path.exists() for path in MANAGED_PATHS), dirty_runner_message(
        request.config.stash.get(MACHINE_WIDE_LEAK, None)
    )
    for tool in ("ug", CLAUDE, CODEX, "databricks"):
        assert shutil.which(tool), f"Install the required CLI before running this CUJ: {tool}"

    class_bearer = bearer(request.cls.workspace)
    run_directory = tmp_path_factory.mktemp(request.cls.__name__)
    artifacts = run_directory / "artifacts"
    print(f"CUJ artifacts: {artifacts}")
    with tempfile.TemporaryDirectory(prefix="ug-cuj-", dir="/tmp") as temporary:
        workspace = Workspace(request.cls.workspace)
        with TuiRequestRecorder(workspace.url) as recorder:
            session = UserSession(
                Path(temporary), Path(shutil.which("ug")), artifacts, class_bearer
            )
            try:
                published = workspace.config()
                try:
                    yield session, workspace, recorder
                finally:
                    try:
                        workspace.assert_unchanged(published)
                    finally:
                        try:
                            session.revert_machine_wide(
                                "cleanup-revert", "CUJ teardown left machine-wide agent settings"
                            )
                        finally:
                            record_machine_wide_leak(request)
            except DatabricksError as error:
                raise _scrubbed(error) from None


@pytest.fixture(autouse=True)
def _refresh_cuj_bearer(request):
    # The class bearer is minted once, but a long class can outlive the ~1h M2M token.
    if "cuj" in request.fixturenames:
        session, _, _ = request.getfixturevalue("cuj")
        try:
            refreshed = bearer(request.cls.workspace)
        except DatabricksError as error:
            raise _scrubbed(error) from None
        session.refresh_bearer(refreshed)


# CI runs a CUJ file slow enough to dominate `e2e` as several shards (tests/live_shard.py); each
# shard keeps only its cases so the shards together run the file exactly once.
_shard_report: dict = {}


def pytest_collection_modifyitems(config, items):
    spec = os.environ.get("UG_LIVE_SHARD")
    if not spec:
        return
    from tests import live_shard

    keep, drop, report = live_shard.plan_items(os.environ["UG_LIVE_SHARD_LANE"], spec, list(items))
    config.hook.pytest_deselected(items=drop)
    items[:] = keep
    _shard_report.update(report)


def pytest_sessionfinish(session, exitstatus):
    if _shard_report and os.environ.get("UG_LIVE_SHARD_REPORT"):
        from tests import live_shard

        live_shard.write_report(os.environ["UG_LIVE_SHARD_REPORT"], _shard_report, {})
