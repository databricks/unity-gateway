"""Local environment/artifact safety checks for CUJ helpers."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e_cuj.helpers import constants
from tests.e2e_cuj.helpers.session import (
    MACHINE_WIDE_LEAK,
    UserSession,
    dirty_runner_message,
    record_machine_wide_leak,
)


def test_cuj_session_does_not_inherit_agent_credentials_or_configuration(tmp_path, monkeypatch):
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SMART_ROUTER_NAME",
        "PYTHONPATH",
        "DATABRICKS_CONFIG_PROFILE",
        "UCODE_MANAGED_CONFIG_STUB",
        "CODEX_HOME",
        "UG_CUJ_SP_CLIENT_ID",
        "UG_CUJ_SP_CLIENT_SECRET",
        "UG_BUDGET_CUJ_SP_CLIENT_ID",
        "UG_BUDGET_CUJ_SP_CLIENT_SECRET",
    ):
        monkeypatch.setenv(name, "developer-value")
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "test-token")
    assert "ANTHROPIC_API_KEY" not in session.env
    assert "OPENAI_API_KEY" not in session.env
    assert "SMART_ROUTER_NAME" not in session.env
    assert "PYTHONPATH" not in session.env
    assert "DATABRICKS_CONFIG_PROFILE" not in session.env
    assert "UCODE_MANAGED_CONFIG_STUB" not in session.env
    assert "UG_CUJ_SP_CLIENT_ID" not in session.env
    assert "UG_CUJ_SP_CLIENT_SECRET" not in session.env
    assert "UG_BUDGET_CUJ_SP_CLIENT_ID" not in session.env
    assert "UG_BUDGET_CUJ_SP_CLIENT_SECRET" not in session.env
    assert session.env["CODEX_HOME"] == str(session.home / ".codex")
    assert not list(session.home.iterdir()), "Fixtures must not manufacture ug/agent state"


def test_cuj_session_redacts_known_bearer_and_authorization_output(tmp_path):
    session = UserSession(tmp_path, Path("/installed/ug"), tmp_path / "artifacts", "known-token")
    redacted = session.redact("known-token Authorization: Bearer another-token")
    assert "known-token" not in redacted and "another-token" not in redacted
    assert redacted.count("<redacted>") == 2


def test_cuj_cleanup_leak_names_the_responsible_scenario(tmp_path, monkeypatch):
    managed_path = tmp_path / "managed-settings.json"
    managed_path.write_text("{}")
    monkeypatch.setattr(constants, "MANAGED_PATHS", (managed_path,))
    request = SimpleNamespace(
        config=SimpleNamespace(stash=pytest.Stash()),
        node=SimpleNamespace(nodeid="CUJ7/family-defaults"),
    )
    record_machine_wide_leak(request)
    message = dirty_runner_message(request.config.stash[MACHINE_WIDE_LEAK])
    assert "CUJ7/family-defaults" in message
    assert str(managed_path) in message
