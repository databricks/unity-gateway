"""Codex CUJs for Tests-table cases 14, 16, 18, 20, 22, and 24."""

import pytest

pytestmark = pytest.mark.codex


@pytest.mark.live
def test_case_14_configured_codex_reuses_saved_model_location(
    live_session, workspace, parent_schema, claude_parent_model, codex_parent_model
):
    """Scenario: configure Codex with --model-location, then launch without options.

    Expected: the saved parent supplies exactly both Claude and Codex Model Services.
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

    assert sorted(models) == sorted([claude_parent_model, codex_parent_model])


@pytest.mark.live
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


@pytest.mark.live
def test_case_18_configured_codex_provider_discovers_models_by_default(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: configure Codex, then launch with --provider.

    Expected: the explicit provider supplies its exact catalog.
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


@pytest.mark.live
def test_case_18_fresh_codex_provider_discovers_models_by_default(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: launch fresh Codex with --workspace and --provider.

    Expected: the explicit provider supplies its exact catalog.
    """
    session = live_session
    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--provider",
            codex_provider,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert models == [codex_provider_model]


@pytest.mark.live
def test_case_20_configured_codex_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, claude_parent_model, codex_parent_model
):
    """Scenario: configure Codex, then launch with --model-location.

    Expected: the explicit parent supplies exactly both Claude and Codex Model Services.
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
        [
            "--model-location",
            parent_schema,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert sorted(models) == sorted([claude_parent_model, codex_parent_model])


@pytest.mark.live
def test_case_20_fresh_codex_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, claude_parent_model, codex_parent_model
):
    """Scenario: launch fresh Codex with --workspace and --model-location.

    Expected: the explicit parent supplies exactly both Claude and Codex Model Services.
    """
    session = live_session
    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--model-location",
            parent_schema,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert sorted(models) == sorted([claude_parent_model, codex_parent_model])


@pytest.mark.live
def test_case_22_configured_codex_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, codex_provider
):
    """Scenario: configure Codex, disable discovery, then launch with --provider.

    Expected: Codex uses the native catalog and ug writes no scoped catalog.
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


@pytest.mark.live
def test_case_22_fresh_codex_provider_uses_native_models_when_discovery_disabled(
    live_session, workspace, codex_provider
):
    """Scenario: disable discovery, then launch fresh Codex with a provider.

    Expected: Codex uses the native catalog and ug writes no scoped catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"
    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--provider",
            codex_provider,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert models
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))


@pytest.mark.live
def test_case_24_configured_codex_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: configure Codex, disable discovery, then launch with a parent.

    Expected: Codex uses the native catalog and ug writes no scoped catalog.
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


@pytest.mark.live
def test_case_24_fresh_codex_location_uses_native_models_when_discovery_disabled(
    live_session, workspace, parent_schema
):
    """Scenario: disable discovery, then launch fresh Codex with a parent.

    Expected: Codex uses the native catalog and ug writes no scoped catalog.
    """
    session = live_session
    session.env["UG_ENABLE_MODEL_DISCOVERY"] = "0"

    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--model-location",
            parent_schema,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    discovered = session.workspace_state()["codex_models"]
    assert models
    assert discovered
    assert all(model.startswith("system.ai.") for model in discovered)
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))
