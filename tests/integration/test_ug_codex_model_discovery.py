"""Codex model-discovery CUJs for repository scenarios 8, 10, 12, and 14."""

import os
import tomllib

import pytest
from utils.provider_catalog import fetch_codex_parent_catalog

pytestmark = [pytest.mark.codex, pytest.mark.usefixtures("unmanaged_workspace")]


@pytest.fixture(scope="module")
def _codex_parent_catalog(unmanaged_workspace, parent_schema, codex_parent_model):
    catalog = fetch_codex_parent_catalog(
        unmanaged_workspace, os.environ["DATABRICKS_BEARER"], parent_schema
    )
    assert codex_parent_model in catalog.model_ids, catalog.model_ids
    assert all(model.startswith(parent_schema + ".") for model in catalog.model_ids), catalog
    return catalog


def _assert_default_models(session, models):
    discovered = session.workspace_state()["codex_models"]
    assert discovered and all(model.startswith("system.ai.") for model in discovered)
    config = tomllib.loads((session.home / ".codex/ucode.config.toml").read_text())
    assert "model" not in config, config
    assert "model_reasoning_effort" not in config, config
    assert "model_catalog_json" not in config, config
    assert models and len(models) == len(set(models)), models
    assert any(model.startswith("gpt-") for model in models), models
    assert not list((session.home / ".ucode").glob("codex-model-catalog-*.json"))


@pytest.mark.live
def test_case_08_configured_codex_uses_default_models(live_session, workspace):
    """Scenario: configure Codex, then launch without source overrides.

    Expected: unmanaged configuration leaves model selection to Codex's native default;
    ug records system.ai discovery while model and reasoning preferences remain unset;
    app-server exposes native GPT entries without a generated provider/parent-scoped catalog.
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

    models = session.codex_model_ids(["app-server", "--listen", "stdio://"])

    _assert_default_models(session, models)


@pytest.mark.live
def test_case_10_fresh_codex_uses_default_models(live_session, workspace):
    """Scenario: launch fresh Codex with --workspace and no source overrides.

    Expected: unmanaged fresh launch leaves model selection to Codex's native default;
    ug records system.ai discovery while model and reasoning preferences remain unset;
    app-server exposes native GPT entries without a generated provider/parent-scoped catalog.
    """
    session = live_session
    models = session.codex_model_ids(
        ["--workspace", workspace, "--", "app-server", "--listen", "stdio://"]
    )

    _assert_default_models(session, models)


@pytest.mark.live
def test_case_12_configured_codex_provider_discovers_models_by_default(
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
def test_case_12_fresh_codex_provider_discovers_models_by_default(
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
def test_case_14_configured_codex_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, _codex_parent_catalog
):
    """Scenario: configure Codex, then launch with --model-location.

    Expected: the app-server list exactly matches the independently fetched parent catalog,
    including the dedicated Codex service and excluding models outside the parent schema.
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

    session.record("case-14-expected-models.json", list(_codex_parent_catalog.model_ids))
    assert sorted(models) == sorted(_codex_parent_catalog.model_ids)


@pytest.mark.live
def test_case_14_fresh_codex_model_location_overrides_saved_setup(
    live_session, workspace, parent_schema, _codex_parent_catalog
):
    """Scenario: launch fresh Codex with --workspace and --model-location.

    Expected: the app-server list exactly matches the independently fetched parent catalog,
    including the dedicated Codex service and excluding models outside the parent schema.
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

    session.record("case-14-expected-models.json", list(_codex_parent_catalog.model_ids))
    assert sorted(models) == sorted(_codex_parent_catalog.model_ids)
