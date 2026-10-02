from pathlib import Path

import pytest

from scripts import run_integration as runner


def windows_live_environment():
    return {"DATABRICKS_BEARER": "bearer"}


@pytest.mark.parametrize("suite", ["integration", "e2e-integration"])
def test_runner_service_principal_credentials_are_scoped_by_suite(suite):
    environment = {
        "DATABRICKS_CLIENT_ID": "legacy-id",
        "DATABRICKS_CLIENT_SECRET": "legacy-secret",
        "UG_CUJ_SP_CLIENT_ID": "cuj-id",
        "UG_CUJ_SP_CLIENT_SECRET": "cuj-secret",
    }
    original = dict(environment)
    expected = (
        ("", "cuj-id", "cuj-secret")
        if suite == "e2e-integration"
        else ("", "legacy-id", "legacy-secret")
    )
    assert runner.workspace_auth_inputs(suite, environment) == expected
    args = runner.arguments(
        ["--suite", suite, "--claude-version", "2.1.280", "--workspace", "https://example.test"],
        platform_name="posix",
        environment=environment,
    )
    assert args.profile is None
    assert not any(value in repr(vars(args)) for value in environment.values())
    assert environment == original


@pytest.mark.parametrize("suite", ["integration", "e2e-integration"])
def test_runner_does_not_use_another_suites_service_principal(suite, capsys):
    other = "integration" if suite == "e2e-integration" else "e2e-integration"
    environment = dict.fromkeys(runner.SERVICE_PRINCIPAL_ENV[other], "unrelated-credential")
    with pytest.raises(SystemExit):
        runner.arguments(
            [
                "--suite",
                suite,
                "--claude-version",
                "2.1.280",
                "--workspace",
                "https://example.test",
            ],
            platform_name="posix",
            environment=environment,
        )
    error = capsys.readouterr().err
    assert runner.SERVICE_PRINCIPAL_ENV[suite][0] in error
    assert "unrelated-credential" not in error


@pytest.mark.parametrize("missing", ["UG_CUJ_SP_CLIENT_ID", "UG_CUJ_SP_CLIENT_SECRET"])
def test_cuj_runner_rejects_partial_credentials_without_falling_back(missing, capsys):
    environment = {
        "UG_CUJ_SP_CLIENT_ID": "cuj-id",
        "UG_CUJ_SP_CLIENT_SECRET": "cuj-secret",
        "DATABRICKS_BEARER": "shared-bearer",
    }
    del environment[missing]
    with pytest.raises(SystemExit):
        runner.arguments(
            [
                "--suite",
                "e2e-integration",
                "--claude-version",
                "2.1.280",
                "--workspace",
                "https://example.test",
            ],
            platform_name="posix",
            environment=environment,
        )
    error = capsys.readouterr().err
    assert "Set both UG_CUJ_SP_CLIENT_ID and UG_CUJ_SP_CLIENT_SECRET" in error
    assert not any(value in error for value in environment.values())


def test_cuj_credentials_override_only_the_shared_bearer():
    environment = {
        "UG_CUJ_SP_CLIENT_ID": "cuj-id",
        "UG_CUJ_SP_CLIENT_SECRET": "cuj-secret",
        "DATABRICKS_BEARER": "shared-bearer",
    }
    assert runner.workspace_auth_inputs("e2e-integration", environment) == (
        "",
        "cuj-id",
        "cuj-secret",
    )
    assert runner.workspace_auth_inputs("integration", environment) == ("shared-bearer", "", "")
    assert runner.workspace_auth_inputs(
        "e2e-integration", {"DATABRICKS_BEARER": "explicit-bearer"}
    ) == ("explicit-bearer", "", "")


@pytest.mark.parametrize("suite", [None, "integration", "e2e-integration"])
def test_cuj_runner_suite_selection_preserves_the_default(suite):
    args = runner.arguments(
        [
            "--claude-version",
            "2.1.280",
            "--workspace",
            "https://example.test",
            *(["--suite", suite] if suite else []),
        ],
        platform_name="posix",
        environment={"DATABRICKS_BEARER": "bearer"},
    )
    assert args.suite == (suite or "integration")
    assert args.pytest_args == ["-m", "live"]


@pytest.mark.parametrize("mode", ["--installation-only", "--headless-only"])
def test_cuj_runner_rejects_legacy_subsets_for_full_e2e(mode, capsys):
    with pytest.raises(SystemExit) as error:
        runner.arguments(
            ["--suite", "e2e-integration", "--claude-version", "2.1.280", mode],
            platform_name="posix",
            environment={},
        )
    assert error.value.code == 2
    assert "belong to --suite integration" in capsys.readouterr().err


def test_installer_environment_scopes_registry_credentials():
    base = {"PATH": "tools", "UV_DEFAULT_INDEX": "databricks-pypi=https://example/simple"}
    source = {
        "UV_INDEX_DATABRICKS_PYPI_USERNAME": "oidc-user",
        "UV_INDEX_DATABRICKS_PYPI_PASSWORD": "python-token",
        "UG_INTEGRATION_NPM_TOKEN": "npm-token",
        "UG_CUJ_SP_CLIENT_ID": "cuj-id",
        "UG_CUJ_SP_CLIENT_SECRET": "cuj-secret",
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
