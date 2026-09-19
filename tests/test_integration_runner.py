"""Local checks for integration-runner deadline configuration."""

import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_integration.py"


@pytest.mark.parametrize("option", ["--test-timeout", "--suite-timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "not-a-duration"])
def test_integration_runner_rejects_invalid_deadlines(option, value):
    result = subprocess.run(
        [sys.executable, str(RUNNER), option, value],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert option in result.stderr
    assert "error:" in result.stderr
