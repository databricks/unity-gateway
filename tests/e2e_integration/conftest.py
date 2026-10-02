"""Independent collection boundary and automatic GitHub Actions discovery."""

import hashlib
import json
from pathlib import Path

import pytest
from helpers.base import BaseCujTest


def pytest_addoption(parser):
    parser.addoption(
        "--cuj-matrix-output",
        type=Path,
        help="Write the collected CUJs as an Actions matrix; requires --collect-only.",
    )


def pytest_configure(config):
    suite = Path(__file__).resolve().parent
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != suite:
        raise pytest.UsageError(
            "Full E2E CUJs require --confcutdir=tests/e2e_integration. "
            "Use scripts/run_integration.py --suite e2e-integration for live runs."
        )
    if config.getoption("cuj_matrix_output") and not config.getoption("collectonly"):
        raise pytest.UsageError("--cuj-matrix-output requires --collect-only.")


def pytest_collection_modifyitems(items):
    workspaces = {}
    for item in items:
        item.add_marker(pytest.mark.dedicated_workspace)
        item.user_properties.extend(
            [("workspace_ownership", "dedicated"), ("configuration", "real_config")]
        )
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


def pytest_collection_finish(session):
    output = session.config.getoption("cuj_matrix_output")
    if output is None:
        return
    if session.testsfailed:
        return
    entries = []
    workspaces = set()
    for item in session.items:
        markers = {mark.name for mark in item.iter_markers()}
        if "live" not in markers or not markers & {"claude", "codex"}:
            raise pytest.UsageError(f"{item.nodeid} needs live and claude/codex markers for CI.")
        workspace = item.cls.WORKSPACE_URL.rstrip("/")
        workspace_key = hashlib.sha256(workspace.lower().encode()).hexdigest()[:16]
        if workspace_key in workspaces:
            raise pytest.UsageError("Assign a separate workspace to each collected CUJ.")
        workspaces.add(workspace_key)
        entries.append(
            {
                "name": item.name,
                "nodeid": item.nodeid,
                "workspace": workspace,
                "workspace_key": workspace_key,
                "artifact_key": hashlib.sha256(item.nodeid.encode()).hexdigest()[:16],
                "claude": "claude" in markers,
                "codex": "codex" in markers,
            }
        )
    if len(entries) > 256:
        raise pytest.UsageError("GitHub Actions allows at most 256 CUJs per matrix.")
    # Pytest still returns NO_TESTS_COLLECTED for an empty scaffold. Do not turn
    # an empty discovery into a successful live workflow.
    output.write_text(json.dumps({"include": entries}) + "\n")
