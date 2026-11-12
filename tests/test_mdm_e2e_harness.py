"""The MDM E2E harness must not print a minted credential, or leave processes, when a command times out."""

import os
import time

import pytest

pytest.importorskip("pwd", reason="The MDM E2E harness runs only on Linux and macOS")

from tests.e2e_mdm import conftest as harness  # noqa: E402


def test_timeout_failure_masks_registered_credentials(monkeypatch):
    token = "registered-secret-value"
    monkeypatch.setattr(harness, "SECRETS", [token])

    with pytest.raises(pytest.fail.Exception) as failure:
        harness.run(
            ["sh", "-c", 'echo "$1"; exec sleep 5', "sh", f"DATABRICKS_BEARER={token}"],
            timeout=0.5,
        )

    message = str(failure.value)
    assert "timed out after 0.5s" in message
    assert "DATABRICKS_BEARER=***" in message
    assert token not in message
    assert failure.value.__context__ is None


def test_output_masks_unregistered_jwt():
    token = "eyJfake-header.fake-payload.fake-signature"
    result = harness.run(["sh", "-c", 'echo "bearer $1"', "sh", token], timeout=10)
    assert result.output.strip() == "bearer ***"


def test_timeout_kills_descendants(tmp_path):
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()

    with pytest.raises(pytest.fail.Exception):
        harness.run(["sh", "-c", 'sleep 30 & echo $! > "$1"; wait', "sh", str(pid_file)], timeout=1)

    # A surviving child keeps the output pipe open, so the timeout would wait for it to exit.
    assert time.monotonic() - started < 10
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"Background child {child} survived the timeout")
