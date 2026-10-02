from pathlib import Path

import pytest

from scripts import run_integration as runner


def windows_live_environment():
    return {"DATABRICKS_BEARER": "bearer"}


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


def test_headless_only_selects_exact_nodes_for_requested_agents(tmp_path):
    assert runner.integration_test_targets(
        tmp_path,
        ["claude", "codex"],
        platform_name="nt",
        installation_only=False,
        headless_only=True,
    ) == [
        f"{tmp_path / 'test_ug_claude_headless.py'}::test_ug_claude_headless_prompt_argument",
        f"{tmp_path / 'test_ug_codex_headless.py'}::test_ug_codex_headless_prompt_argument",
    ]


def test_headless_only_is_mutually_exclusive_with_installation_only():
    with pytest.raises(SystemExit):
        runner.arguments(
            [
                "--claude-version",
                "2.1.268",
                "--installation-only",
                "--headless-only",
            ],
            platform_name="nt",
            environment={},
        )


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--headless-only"], windows_live_environment()),
        (["--headless-only", "--workspace", "https://example.test"], {}),
    ],
)
def test_headless_only_requires_live_workspace_and_auth(arguments, environment):
    with pytest.raises(SystemExit):
        runner.arguments(
            ["--claude-version", "2.1.268", *arguments],
            platform_name="nt",
            environment=environment,
        )


def test_headless_only_is_allowed_on_windows_with_live_workspace_and_auth():
    args = runner.arguments(
        [
            "--claude-version",
            "2.1.268",
            "--headless-only",
            "--workspace",
            "https://example.test",
        ],
        platform_name="nt",
        environment=windows_live_environment(),
    )

    assert args.headless_only is True
    assert args.installation_only is False
    assert args.pytest_args == ["-m", "live"]


def test_windows_policy_paths_match_pinned_agent_locations():
    paths = runner.managed_policy_paths(
        ["claude", "codex"],
        {"PROGRAMDATA": "C:/ProgramData", "PROGRAMFILES": "C:/Program Files"},
        platform_name="nt",
        system_platform="win32",
    )

    assert paths == (
        Path("C:/ProgramData/OpenAI/Codex/requirements.toml"),
        Path("C:/ProgramData/OpenAI/Codex/config.toml"),
        Path("C:/Program Files/ClaudeCode/managed-settings.json"),
    )


def test_windows_policy_preflight_fails_closed_without_required_roots():
    with pytest.raises(RuntimeError, match="PROGRAMDATA"):
        runner.managed_policy_paths(["codex"], {}, platform_name="nt", system_platform="win32")
