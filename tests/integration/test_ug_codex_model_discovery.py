"""CUJs: Codex model discovery through explicit parent and provider scopes."""

import pytest

pytestmark = [pytest.mark.live, pytest.mark.codex]


def test_ug_codex_fresh_provider_discovers_only_openai_mps_models(
    live_session, workspace, codex_provider, codex_provider_model
):
    """Scenario: launch Codex with an OpenAI MPS from a fresh home.

    Expected: Codex's real model/list response contains only that MPS's model.
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
    session.assert_not_routed()


def test_ug_codex_fresh_parent_discovers_only_parent_models(
    live_session, workspace, parent_schema, codex_parent_model
):
    """Scenario: launch Codex with a Unity Catalog parent from a fresh home.

    Expected: Codex's real model/list response contains only the compatible
    Model Service in that schema.
    """
    session = live_session

    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--parent",
            parent_schema,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert models == [codex_parent_model]
    session.assert_not_routed()


def test_ug_codex_configured_scope_switch_refreshes_provider_then_parent_models(
    live_session,
    workspace,
    codex_provider,
    codex_provider_model,
    parent_schema,
    codex_parent_model,
):
    """Scenario: configure Hosted Codex, then launch with MPS and parent scopes.

    Expected: each explicit scope overrides the saved setup, and model/list
    reflects the current scope rather than a catalog from the previous launch.
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

    provider_models = session.codex_model_ids(
        [
            "--provider",
            codex_provider,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ],
        name="provider-models",
    )
    assert provider_models == [codex_provider_model]

    parent_models = session.codex_model_ids(
        [
            "--parent",
            parent_schema,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ],
        name="parent-models",
    )
    assert parent_models == [codex_parent_model]
    session.assert_not_routed()


def test_ug_codex_bedrock_provider_discovers_only_openai_targets(
    live_session, workspace, bedrock_provider, bedrock_codex_model
):
    """Scenario: launch Codex with a mixed-model Bedrock MPS from a fresh home.

    Expected: Codex's real model/list response contains only its compatible
    OpenAI Responses target.
    """
    session = live_session

    models = session.codex_model_ids(
        [
            "--workspace",
            workspace,
            "--provider",
            bedrock_provider,
            "--",
            "app-server",
            "--listen",
            "stdio://",
        ]
    )

    assert models == [bedrock_codex_model]
    session.assert_not_routed()


def test_ug_codex_rejects_missing_provider(live_session, workspace):
    """Scenario: launch Codex with a provider name that does not exist.

    Expected: ug returns a clear not-found error before starting Codex.
    """
    missing = "does.not.exist"
    result = live_session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        missing,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    assert result.returncode == 1
    assert f"Model provider service '{missing}' was not found" in result.stdout + result.stderr


def test_ug_codex_rejects_wrong_provider_type(live_session, workspace, claude_provider):
    """Scenario: launch Codex with an Anthropic Model Provider Service.

    Expected: ug rejects the incompatible provider before starting Codex.
    """
    result = live_session.run(
        "codex",
        "--workspace",
        workspace,
        "--provider",
        claude_provider,
        "--",
        "--version",
        ok=False,
        timeout=240,
    )

    assert result.returncode == 1
    assert "which codex can't route to" in result.stdout + result.stderr


def test_ug_codex_rejects_malformed_parent(live_session):
    """Scenario: launch Codex with a one-part parent value.

    Expected: ug rejects it as malformed before starting Codex.
    """
    result = live_session.run("codex", "--parent", "main", ok=False)

    assert result.returncode == 1
    assert "--parent must be `<catalog>.<schema>`" in result.stdout + result.stderr


def test_ug_codex_rejects_provider_and_parent_together(live_session, codex_provider, parent_schema):
    """Scenario: launch Codex with both mutually exclusive discovery scopes.

    Expected: ug rejects the conflicting options before starting Codex.
    """
    result = live_session.run(
        "codex",
        "--provider",
        codex_provider,
        "--parent",
        parent_schema,
        ok=False,
    )

    assert result.returncode == 1
    assert "--provider and --parent cannot be used together" in result.stdout + result.stderr
