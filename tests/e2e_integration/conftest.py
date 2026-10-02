"""Independent collection boundary; live fixtures will be added with the CUJs."""

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
        workspace = item.cls.WORKSPACE_URL
        if not workspace:
            raise pytest.UsageError(f"{item.cls.__name__} must declare WORKSPACE_URL.")
        if workspace in workspaces:
            raise pytest.UsageError(
                f"{item.nodeid} and {workspaces[workspace]} share a Databricks workspace. "
                "Assign a separate WORKSPACE_URL to each test."
            )
        workspaces[workspace] = item.nodeid
