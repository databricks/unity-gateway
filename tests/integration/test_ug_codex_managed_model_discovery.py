"""Codex managed-config CUJs for Tests-table cases 2, 4, 6, 8, 10, and 12.

The admin CodingAgentConfig input is injected through ``UCODE_MANAGED_CONFIG_STUB``. Authentication,
normalization, config writers, the gateway, and Codex remain real. The un-stubbed managed configure
journey covers the fetch/wire contract.
"""

import pytest
from utils.constants import MANAGED_FIXTURE_CODEX_MODELS
from utils.managed import (
    build_codex_agent_config,
    build_coding_agent_config,
    is_managed_config_control_plane_cache,
    set_managed_config_stub,
)

pytestmark = [pytest.mark.managed_fixture, pytest.mark.codex]

CODEX_MANAGED_CONFIG = build_coding_agent_config(
    "CODING_AGENT_CODEX",
    build_codex_agent_config(models=MANAGED_FIXTURE_CODEX_MODELS),
)


@pytest.fixture(autouse=True)
def _managed_codex_config(live_session, tmp_path):
    set_managed_config_stub(live_session, tmp_path, CODEX_MANAGED_CONFIG)


def _codex_state_and_agent_files(session):
    paths = []
    for directory in (session.home / ".ucode", session.home / ".codex"):
        if directory.exists():
            paths.extend(path for path in directory.rglob("*") if path.is_file())
    return {
        str(path.relative_to(session.home)): path.read_bytes()
        for path in paths
        if path.is_file()
        # A fresh launch must retrieve and cache the control-plane input before it can reject an
        # override. Exclude only that expected cache; every agent-owned state/file stays compared.
        and not is_managed_config_control_plane_cache(session.home, path)
    }


def test_case_02_managed_codex_uses_admin_discovery_after_configure(live_session, workspace):
    """Scenario: configure managed Codex, then launch its app server.

    Expected: Codex exposes exactly the admin-managed model catalog.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])

    assert models == MANAGED_FIXTURE_CODEX_MODELS


def test_case_02_managed_codex_uses_admin_discovery_from_fresh_state(live_session, workspace):
    """Scenario: launch managed Codex with --workspace from fresh state.

    Expected: Codex exposes exactly the admin-managed model catalog.
    """
    session = live_session
    models = session.codex_model_ids(
        ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )

    assert models == MANAGED_FIXTURE_CODEX_MODELS


def test_case_04_managed_codex_ignores_discovery_disable_after_configure(live_session, workspace):
    """Scenario: configure managed Codex, disable discovery, then launch its app server.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])

    assert models == MANAGED_FIXTURE_CODEX_MODELS


def test_case_04_managed_codex_ignores_discovery_disable_from_fresh_state(live_session, workspace):
    """Scenario: disable discovery and launch managed Codex with --workspace from fresh state.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    models = session.codex_model_ids(
        ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )

    assert models == MANAGED_FIXTURE_CODEX_MODELS


def test_case_06_managed_codex_rejects_provider_override_after_configure(
    live_session, workspace, codex_provider
):
    """Scenario: configure managed Codex, then pass a --provider override.

    Expected: ug rejects the override without changing agent-owned state or files.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert f"provider {codex_provider}".lower() in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_06_managed_codex_rejects_provider_override_from_fresh_state(
    live_session, workspace, codex_provider
):
    """Scenario: pass --workspace and a --provider override from fresh state.

    Expected: ug rejects the override without changing agent-owned state or files, apart from the
    managed-config retrieval cache.
    """
    session = live_session
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert f"provider {codex_provider}".lower() in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_08_managed_codex_rejects_model_location_override_after_configure(
    live_session, workspace, parent_schema
):
    """Scenario: configure managed Codex, then pass a --model-location override.

    Expected: ug rejects the override without changing agent-owned state or files.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "--model-location" in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_08_managed_codex_rejects_model_location_override_from_fresh_state(
    live_session, workspace, parent_schema
):
    """Scenario: pass --workspace and a --model-location override from fresh state.

    Expected: ug rejects the override without changing agent-owned state or files, apart from the
    managed-config retrieval cache.
    """
    session = live_session
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "--model-location" in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_10_managed_codex_rejects_provider_when_discovery_disabled_after_configure(
    live_session, workspace, codex_provider
):
    """Scenario: configure managed Codex, disable discovery, then pass a --provider override.

    Expected: ug rejects the override without changing agent-owned state or files.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert f"provider {codex_provider}".lower() in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_10_managed_codex_rejects_provider_when_discovery_disabled_from_fresh_state(
    live_session, workspace, codex_provider
):
    """Scenario: disable discovery and pass --workspace plus --provider from fresh state.

    Expected: ug rejects the override without changing agent-owned state or files, apart from the
    managed-config retrieval cache.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert f"provider {codex_provider}".lower() in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_12_managed_codex_rejects_model_location_when_discovery_disabled_after_configure(
    live_session, workspace, parent_schema
):
    """Scenario: configure managed Codex, disable discovery, then pass --model-location.

    Expected: ug rejects the override without changing agent-owned state or files.
    """
    session = live_session
    configured = session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    assert "Select coding agents to configure:" not in configured.stdout, configured.stdout
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "--model-location" in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before


def test_case_12_managed_codex_rejects_model_location_when_discovery_disabled_from_fresh_state(
    live_session, workspace, parent_schema
):
    """Scenario: disable discovery and pass --workspace plus --model-location from fresh state.

    Expected: ug rejects the override without changing agent-owned state or files, apart from the
    managed-config retrieval cache.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    before = _codex_state_and_agent_files(session)

    result = session.run(
        "codex",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "--model-location" in output
    assert "admin has specified managed" in output
    assert _codex_state_and_agent_files(session) == before
