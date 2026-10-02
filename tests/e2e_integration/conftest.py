"""Independent collection boundary; live fixtures will be added with the CUJs."""

from pathlib import Path

import pytest


def pytest_configure(config):
    suite = Path(__file__).resolve().parent
    boundary = config.getoption("confcutdir")
    if boundary is None or Path(boundary).resolve() != suite:
        raise pytest.UsageError(
            "Full E2E CUJs require --confcutdir=tests/e2e_integration. "
            "Use scripts/run_integration.py --suite e2e-integration for live runs."
        )
