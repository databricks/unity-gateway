"""Red CUJs for every Codex row in the model-discovery Tests table."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.codex]


def test_case_02_managed_codex_uses_admin_discovery_after_configure(
    managed_live_session, managed_workspace, managed_codex_model
):
    """Scenario: configure Codex, then launch in a workspace with managed discovery.

    Expected: the managed model catalog wins over the developer's saved setup.
    """
    session = managed_live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        managed_workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])

    assert models == [managed_codex_model]


def test_case_04_managed_codex_ignores_discovery_disable(
    managed_live_session, managed_workspace, managed_codex_model
):
    """Scenario: launch managed Codex with UG_ENABLE_MODEL_DISCOVERY=0.

    Expected: workspace-managed discovery still supplies the admin's catalog.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    models = session.codex_model_ids(
        ["--workspace", managed_workspace, "--", "app-server", "--listen", "stdio://"]
    )

    assert models == [managed_codex_model]


def test_case_06_managed_codex_rejects_provider_override(
    managed_live_session, managed_workspace, codex_provider
):
    """Scenario: pass --provider when the workspace manages Codex discovery.

    Expected: ug rejects the developer override before Codex starts.
    """
    result = managed_live_session.run(
        "codex",
        "--workspace",
        managed_workspace,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_08_managed_codex_rejects_model_location_override(
    managed_live_session, managed_workspace, parent_schema
):
    """Scenario: pass --model-location when the workspace manages Codex discovery.

    Expected: ug rejects the developer override before Codex starts.
    """
    result = managed_live_session.run(
        "codex",
        "--workspace",
        managed_workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_10_managed_codex_rejects_provider_when_discovery_disabled(
    managed_live_session, managed_workspace, codex_provider
):
    """Scenario: disable discovery and pass --provider in a managed workspace.

    Expected: the managed-config override remains invalid and ug rejects it.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    result = session.run(
        "codex",
        "--workspace",
        managed_workspace,
        "--provider",
        codex_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_12_managed_codex_rejects_model_location_when_discovery_disabled(
    managed_live_session, managed_workspace, parent_schema
):
    """Scenario: disable discovery and pass --model-location in a managed workspace.

    Expected: the managed-config override remains invalid and ug rejects it.
    """
    session = managed_live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    result = session.run(
        "codex",
        "--workspace",
        managed_workspace,
        "--model-location",
        parent_schema,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    output = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "managed" in output or "admin" in output


def test_case_14_configured_codex_reuses_saved_model_location(
    live_session, workspace, parent_schema, codex_parent_model
):
    """Scenario: configure Codex with --model-location, then launch without options.

    Expected: the saved parent supplies Codex's discovered model catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--model-location",
        parent_schema,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])

    assert models == [codex_parent_model]


def test_case_16_fresh_codex_uses_system_models_when_discovery_disabled(live_session, workspace):
    """Scenario: launch fresh Codex with UG_ENABLE_MODEL_DISCOVERY=0.

    Expected: ug uses its discovered system.ai models without a scoped catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    models = session.codex_model_ids(
        ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )

    discovered = session.workspace_state()["codex_models"]
    assert models
    assert discovered
    assert all(model.startswith("system.ai.") for model in discovered)
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))


def test_case_18_configured_codex_provider_discovers_models_by_default(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: configure Hosted Codex, then launch with --provider.

    Expected: the explicit provider overrides setup and supplies its exact catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    models = session.codex_model_ids(
        ["--provider", codex_provider, "--", "app-server", "--listen", "stdio://"]
    )

    assert models == [codex_provider_model]


def test_case_20_configured_codex_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, codex_parent_model
):
    """Scenario: configure Hosted Codex, then launch with --model-location.

    Expected: the explicit parent overrides the saved Hosted configuration.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    models = session.codex_model_ids(
        ["--model-location", parent_schema, "--", "app-server", "--listen", "stdio://"]
    )

    assert models == [codex_parent_model]


def test_case_22_codex_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, codex_provider
):
    """Scenario: configure Codex, disable discovery, then launch with --provider.

    Expected: Codex uses its native catalog and ug writes no scoped catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    models = session.codex_model_ids(
        ["--provider", codex_provider, "--", "app-server", "--listen", "stdio://"]
    )

    assert models
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))


def test_case_24_codex_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: configure Codex, disable discovery, then pass --model-location.

    Expected: Codex uses its native catalog and ug writes no scoped catalog.
    """
    session = live_session
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    models = session.codex_model_ids(
        ["--model-location", parent_schema, "--", "app-server", "--listen", "stdio://"]
    )

    discovered = session.workspace_state()["codex_models"]
    assert models
    assert discovered
    assert all(model.startswith("system.ai.") for model in discovered)
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))
