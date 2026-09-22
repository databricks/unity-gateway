"""Codex managed-config CUJs for repository scenarios 2, 4, and 6.

The admin CodingAgentConfig is fetched once from the managed workspace, its Codex model source is
set to the dedicated test MPS, and the result is reused through ``UCODE_MANAGED_CONFIG_STUB`` in
each isolated session. Normalization, config writers, the gateway, and Codex remain real.
"""

import json
import os
import tomllib

import pytest
from utils.constants import MANAGED_CODEX_PROVIDER_SERVICE
from utils.managed import (
    fetch_managed_config_stub,
    is_managed_config_control_plane_cache,
    use_managed_config_stub,
)
from utils.provider_catalog import (
    CodexProviderCatalog,
    fetch_codex_provider_catalog,
    parse_codex_provider_catalog,
)

pytestmark = [pytest.mark.managed_fixture, pytest.mark.codex]


@pytest.fixture(scope="module")
def _managed_codex_config_stub(workspace, tmp_path_factory):
    return fetch_managed_config_stub(
        workspace,
        os.environ["DATABRICKS_BEARER"],
        tmp_path_factory.mktemp("managed-config-codex"),
        "managed-config-codex.json",
        agent="CODING_AGENT_CODEX",
        provider_service=MANAGED_CODEX_PROVIDER_SERVICE,
    )


@pytest.fixture(scope="module")
def _managed_codex_provider_catalog(workspace):
    return fetch_codex_provider_catalog(
        workspace,
        os.environ["DATABRICKS_BEARER"],
        MANAGED_CODEX_PROVIDER_SERVICE,
    )


@pytest.fixture(autouse=True)
def _managed_codex_config(live_session, _managed_codex_config_stub):
    use_managed_config_stub(live_session, _managed_codex_config_stub)


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
        # override. Exclude that expected cache and Codex's disposable arg0 executable links,
        # which even a version check recreates; persistent agent-owned files stay compared.
        and not is_managed_config_control_plane_cache(session.home, path)
        and not path.is_relative_to(session.home / ".codex" / "tmp" / "arg0")
    }


def _assert_rejected_before_codex_started(session, result, before=None):
    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "`--provider` or `--model-location` is not allowed" in output
    assert "managed config exists for the workspace" in output
    if before is not None:
        assert _codex_state_and_agent_files(session) == before


def _assert_managed_provider_catalog(session, models, expected: CodexProviderCatalog):
    config = tomllib.loads((session.home / ".codex" / "ucode.config.toml").read_text())
    # The provider header is a launch-only overlay; neither it nor the scoped catalog is persisted
    # in Codex's generated profile.
    assert "model_catalog_json" not in config, config

    managed_cache = json.loads((session.home / ".ucode/managed-config.json").read_text())
    raw_config = managed_cache.get("config")
    enabled_agents = raw_config.get("enabled_agents") if isinstance(raw_config, dict) else None
    assert isinstance(enabled_agents, list), "persisted managed config had no enabled_agents list"
    codex_entries = [
        entry
        for entry in enabled_agents
        if isinstance(entry, dict) and entry.get("agent") == "CODING_AGENT_CODEX"
    ]
    assert len(codex_entries) == 1, "persisted managed config did not contain one Codex entry"
    managed_codex = codex_entries[0].get("config")
    assert isinstance(managed_codex, dict), "persisted managed Codex config was not an object"
    assert managed_codex.get("models") == {
        "model_provider_service": MANAGED_CODEX_PROVIDER_SERVICE
    }, "persisted managed Codex config did not select the dedicated MPS"
    assert "default_models" not in managed_codex, "static Codex defaults survived the MPS variant"

    catalog_paths = list((session.home / ".ucode").glob("codex-model-catalog-*.json"))
    assert len(catalog_paths) == 1, catalog_paths
    catalog = json.loads(catalog_paths[0].read_text())
    catalog_ids = parse_codex_provider_catalog(catalog)
    session.record(
        "managed-provider-catalog.json",
        {
            "provider": MANAGED_CODEX_PROVIDER_SERVICE,
            "expected_model_ids": list(expected.model_ids),
            "generated_catalog_model_ids": list(catalog_ids),
            "app_server_model_ids": models,
        },
    )
    session.record("managed-codex-catalog.json", catalog)
    assert isinstance(models, list) and models, models
    assert all(isinstance(model_id, str) and model_id for model_id in models), models
    assert len(models) == len(set(models)), models
    assert sorted(catalog_ids) == sorted(expected.model_ids), (catalog_ids, expected.model_ids)
    assert sorted(models) == sorted(expected.model_ids), (models, expected.model_ids)
    assert models == list(catalog_ids), (models, catalog)


def test_case_02_managed_codex_uses_admin_discovery_after_configure(
    live_session, workspace, _managed_codex_provider_catalog
):
    """Scenario: configure managed Codex, then launch its app server.

    Expected: the independently fetched provider catalog exactly matches both the generated
    catalog and the app-server model list.
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

    _assert_managed_provider_catalog(session, models, _managed_codex_provider_catalog)


def test_case_02_managed_codex_uses_admin_discovery_from_fresh_state(
    live_session, workspace, _managed_codex_provider_catalog
):
    """Scenario: launch managed Codex with --workspace from fresh state.

    Expected: the independently fetched provider catalog exactly matches both the generated
    catalog and the app-server model list.
    """
    session = live_session
    models = session.codex_model_ids(
        ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )

    _assert_managed_provider_catalog(session, models, _managed_codex_provider_catalog)


def test_case_04_managed_codex_rejects_provider_override_after_configure(
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

    _assert_rejected_before_codex_started(session, result, before)


def test_case_04_managed_codex_rejects_provider_override_from_fresh_state(
    live_session, workspace, codex_provider
):
    """Scenario: pass --workspace and a --provider override from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Codex.
    """
    session = live_session
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

    _assert_rejected_before_codex_started(session, result)


def test_case_06_managed_codex_rejects_model_location_override_after_configure(
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

    _assert_rejected_before_codex_started(session, result, before)


def test_case_06_managed_codex_rejects_model_location_override_from_fresh_state(
    live_session, workspace, parent_schema
):
    """Scenario: pass --workspace and a --model-location override from fresh state.

    Expected: ug may establish the fresh workspace/agent configuration, then rejects the
    override before starting Codex.
    """
    session = live_session
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

    _assert_rejected_before_codex_started(session, result)
