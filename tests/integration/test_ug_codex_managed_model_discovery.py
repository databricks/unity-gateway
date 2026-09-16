"""Codex managed-config CUJs for Tests-table cases 2, 4, 6, 8, 10, and 12."""

import pytest
from utils.constants import MANAGED_CODEX_MODELS

pytestmark = [pytest.mark.managed, pytest.mark.codex]


def _codex_state_and_agent_files(session):
    paths = []
    for directory in (session.home / ".ucode", session.home / ".codex"):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return {
        str(path.relative_to(session.home)): path.read_bytes() for path in paths if path.is_file()
    }


def _configure_managed(session, workspace):
    result = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in result.stdout, result.stdout


def _assert_rejected_before_codex_started(session, result, requested_source, before):
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert requested_source.lower() in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_02_managed_codex_uses_admin_discovery_after_configure(
    live_session, workspace, configured
):
    """Scenario: launch managed Codex after configure and from fresh state.

    Expected: the managed model catalog wins in both command variants.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)

    args = (
        ["app-server", "--listen", "stdio://"]
        if configured
        else ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )
    models = session.codex_model_ids(args)

    assert models == MANAGED_CODEX_MODELS


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_04_managed_codex_ignores_discovery_disable(live_session, workspace, configured):
    """Scenario: launch configured and fresh managed Codex with discovery disabled.

    Expected: workspace-managed discovery supplies the admin's catalog in both variants.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    args = (
        ["app-server", "--listen", "stdio://"]
        if configured
        else ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )
    models = session.codex_model_ids(args)

    assert models == MANAGED_CODEX_MODELS


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_06_managed_codex_rejects_provider_override(
    live_session, workspace, codex_provider, configured
):
    """Scenario: pass --provider after managed configure and from fresh state.

    Expected: ug rejects both variants without changing state or agent cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    before = _codex_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "codex",
        *args,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_codex_started(session, result, f"provider {codex_provider}", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_08_managed_codex_rejects_model_location_override(
    live_session, workspace, parent_schema, configured
):
    """Scenario: pass --model-location after managed configure and from fresh state.

    Expected: ug rejects both variants without changing state or agent cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    before = _codex_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "codex",
        *args,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_codex_started(session, result, "--model-location", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_10_managed_codex_rejects_provider_when_discovery_disabled(
    live_session, workspace, codex_provider, configured
):
    """Scenario: disable discovery and pass --provider in both managed variants.

    Expected: ug rejects both variants without changing state or agent cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "codex",
        *args,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_codex_started(session, result, f"provider {codex_provider}", before)


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "fresh"])
def test_case_12_managed_codex_rejects_model_location_when_discovery_disabled(
    live_session, workspace, parent_schema, configured
):
    """Scenario: disable discovery and pass --model-location in both managed variants.

    Expected: ug rejects both variants without changing state or agent cache files.
    """
    session = live_session
    if configured:
        _configure_managed(session, workspace)
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)
    args = [] if configured else ["--workspace", workspace]
    result = session.run(
        "codex",
        *args,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    _assert_rejected_before_codex_started(session, result, "--model-location", before)
