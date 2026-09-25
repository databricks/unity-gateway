from pathlib import Path

import pytest

from scripts import run_integration as runner


def test_installer_environment_scopes_registry_credentials():
    base = {"PATH": "tools", "UV_DEFAULT_INDEX": "databricks-pypi=https://example/simple"}
    source = {
        "UV_INDEX_DATABRICKS_PYPI_USERNAME": "oidc-user",
        "UV_INDEX_DATABRICKS_PYPI_PASSWORD": "python-token",
        "UG_INTEGRATION_NPM_TOKEN": "npm-token",
        "UNRELATED_SECRET": "must-not-cross-the-boundary",
    }

    python_environment = runner.installer_environment(base, source, runner.UV_INDEX_CREDENTIAL_ENV)
    npm_environment = runner.installer_environment(
        base, source, (runner.NPM_TOKEN_ENV,), Path("isolated.npmrc")
    )

    assert base == {
        "PATH": "tools",
        "UV_DEFAULT_INDEX": "databricks-pypi=https://example/simple",
    }
    assert python_environment == {
        **base,
        "UV_INDEX_DATABRICKS_PYPI_USERNAME": "oidc-user",
        "UV_INDEX_DATABRICKS_PYPI_PASSWORD": "python-token",
    }
    assert npm_environment == {
        **base,
        "UG_INTEGRATION_NPM_TOKEN": "npm-token",
        "npm_config_userconfig": "isolated.npmrc",
    }


def test_npm_user_config_references_token_environment_without_embedding_it():
    config = runner.npm_user_config("https://databricks.jfrog.io/artifactory/api/npm/db-npm/")

    assert config == (
        "registry=https://databricks.jfrog.io/artifactory/api/npm/db-npm/\n"
        "//databricks.jfrog.io/artifactory/api/npm/db-npm/:_authToken="
        "${UG_INTEGRATION_NPM_TOKEN}\n"
        "always-auth=true\n"
    )


def test_npm_user_config_rejects_credentials_in_registry_url():
    with pytest.raises(ValueError, match="must not contain credentials"):
        runner.npm_user_config("https://user:token@example.invalid/npm/")


def test_installer_tokens_are_redacted_from_build_output():
    output = "failed for oidc-user with python-token and npm-token"

    assert (
        runner.redact_secrets(output, ("oidc-user", "python-token", "npm-token"))
        == "failed for <redacted> with <redacted> and <redacted>"
    )
