"""The MDM E2E harness must not print a minted credential when a command times out."""

import pytest

from tests.e2e_mdm import conftest as harness


def test_timeout_failure_masks_registered_credentials(monkeypatch):
    token = "eyJfake-header.fake-payload.fake-signature"
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
