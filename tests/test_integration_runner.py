import base64
import json
import urllib.error
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import call, patch

import pytest

from scripts import run_integration as runner
from tests.integration.utils.harness import UserSession, clean_environment

ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
CUJ5_TARGET_ENV = {
    "UCODE_TEST_WORKSPACE": "https://cuj5-workspace.example",
    "UG_BUDGET_WORKSPACE_ID": "workspace-id",
    "UG_BUDGET_ACCOUNT_HOST": "https://accounts.example",
    "UG_BUDGET_ACCOUNT_ID": "account-id",
    "UG_BUDGET_ID": "budget-id",
    "UG_BUDGET_SONNET_MODEL": "sonnet-model",
    "UG_BUDGET_SOL_MODEL": "sol-model",
    "UG_BUDGET_LUNA_MODEL": "luna-model",
}


def test_mint_m2m_token_uses_workspace_token_endpoint():
    with patch.object(runner.urllib.request, "urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = (
            b'{"access_token":"workspace-token"}'
        )
        token = runner.mint_m2m_token(
            "https://workspace.example/",
            "client",
            "secret",
        )
    assert token == "workspace-token"
    token_request = request.call_args.args[0]
    assert token_request.full_url == "https://workspace.example/oidc/v1/token"
    assert token_request.get_method() == "POST"


def test_mint_account_m2m_token_uses_account_token_endpoint():
    with patch.object(runner.urllib.request, "urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = (
            b'{"access_token":"account-token"}'
        )
        token = runner.mint_account_m2m_token(
            "https://accounts.cloud.databricks.com",
            "client",
            "secret",
            account_id=ACCOUNT_ID,
        )
    assert token == "account-token"
    token_request = request.call_args.args[0]
    assert token_request.full_url == (
        f"https://accounts.cloud.databricks.com/oidc/accounts/{ACCOUNT_ID}/v1/token"
    )
    assert token_request.get_method() == "POST"
    assert (
        token_request.headers["Authorization"]
        == "Basic " + base64.b64encode(b"client:secret").decode()
    )
    assert token_request.headers["Content-type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs(token_request.data.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["all-apis"],
    }


def test_mint_account_m2m_token_quotes_account_id_path_segment():
    with patch.object(runner.urllib.request, "urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = (
            b'{"access_token":"account-token"}'
        )
        token = runner.mint_account_m2m_token(
            "https://accounts.cloud.databricks.com",
            "client",
            "secret",
            account_id="account/id?query",
        )
    assert token == "account-token"
    assert request.call_args.args[0].full_url == (
        "https://accounts.cloud.databricks.com/oidc/accounts/account%2Fid%3Fquery/v1/token"
    )


@pytest.mark.skipif(runner.os.name != "posix", reason="POSIX process-group interrupt handling")
def test_budget_cleanup_receives_its_interrupt_grace_period():
    with (
        patch.object(runner.subprocess, "Popen") as start,
        patch.object(runner.os, "killpg"),
    ):
        with runner.managed_process(["pytest"], interrupt=True, interrupt_grace=180):
            pass
    assert start.return_value.wait.call_args_list == [call(timeout=180), call(timeout=5)]


class _FakeProcess:
    returncode = 0

    def __init__(self, command):
        self.command = command

    def communicate(self, timeout=None):
        if "-c" in self.command:
            runtime = Path(self.command[0]).parent.parent
            output = json.dumps(
                {
                    "distribution": "unity-gateway",
                    "version": "test",
                    "path": str(runtime / "site-packages/ucode/__init__.py"),
                }
            )
        elif "ls" in self.command and "--json" in self.command:
            output = '{"dependencies":{}}'
        elif "--version" in self.command:
            output = "1.2.3"
        else:
            output = ""
        return output, ""

    def wait(self, timeout=None):
        return self.returncode


def _main_setup(
    tmp_path,
    monkeypatch,
    *,
    workspace,
    client_id,
    client_secret,
    target_environment=None,
):
    wheel = tmp_path / "unity_gateway.whl"
    wheel.write_bytes(b"wheel")
    npm_lock = tmp_path / "npm-lock.json"
    npm_lock.write_text("{}")
    arguments_environment = {
        "DATABRICKS_CLIENT_ID": client_id,
        "DATABRICKS_CLIENT_SECRET": client_secret,
    }
    args = runner.arguments(
        [
            "--ug-wheel",
            str(wheel),
            "--npm-lock",
            str(npm_lock),
            "--claude-version",
            "1.2.3",
            "--headless-only",
            "--workspace",
            workspace,
        ],
        environment=arguments_environment,
    )
    args.output = tmp_path / "run"
    monkeypatch.setattr(runner, "arguments", lambda: args)
    monkeypatch.setattr(runner.signal, "signal", lambda *ignored: None)
    monkeypatch.setattr(runner, "present_policy_paths", lambda paths: [])

    binaries = {}
    for name in ("uv", "npm", "node", "databricks"):
        binary = tmp_path / name
        binary.write_text("")
        binaries[name] = binary
    monkeypatch.setattr(runner.shutil, "which", lambda name: str(binaries[name]))

    def fake_venv_executable(environment_path, name):
        executable = Path(environment_path) / "bin" / name
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        return executable

    monkeypatch.setattr(runner, "venv_executable", fake_venv_executable)
    monkeypatch.setattr(runner, "npm_executable", lambda bin_dir, name: Path(bin_dir) / name)

    mint_calls = []
    account_calls = []

    def fake_mint(workspace, client_id, client_secret):
        mint_calls.append((workspace, client_id, client_secret))
        return "workspace-token"

    monkeypatch.setattr(runner, "mint_m2m_token", fake_mint)

    def fake_account_token(account_host, client_id, client_secret, *, account_id):
        account_calls.append((account_host, client_id, client_secret, account_id))
        return "account-token"

    monkeypatch.setattr(runner, "mint_account_m2m_token", fake_account_token)
    process_calls = []

    @contextmanager
    def fake_managed_process(command, **kwargs):
        command = [str(item) for item in command]
        process_calls.append((command, kwargs))
        junit = next((item for item in command if item.startswith("--junitxml=")), None)
        if junit:
            Path(junit.split("=", 1)[1]).write_text(
                '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0" />'
                "</testsuites>"
            )
        yield _FakeProcess(command)

    monkeypatch.setattr(runner, "managed_process", fake_managed_process)

    host_environment = {
        key: runner.os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "PROGRAMDATA",
            "PROGRAMFILES",
        )
        if key in runner.os.environ
    }
    environment = {
        **host_environment,
        **(target_environment or {}),
        "DATABRICKS_CLIENT_ID": client_id,
    }
    environment["DATABRICKS_CLIENT_SECRET"] = client_secret
    return args, environment, mint_calls, account_calls, process_calls


def _pytest_call(process_calls):
    pytest_calls = [
        (command, kwargs)
        for command, kwargs in process_calls
        if "-m" in command and "pytest" in command
    ]
    assert len(pytest_calls) == 1
    return pytest_calls[0][1]


def test_main_forwards_budget_target_into_pytest_only(tmp_path, monkeypatch):
    workspace = CUJ5_TARGET_ENV["UCODE_TEST_WORKSPACE"]
    args, environment, mint_calls, account_calls, process_calls = _main_setup(
        tmp_path,
        monkeypatch,
        workspace=workspace,
        client_id="cuj-client",
        client_secret="cuj-secret",
        target_environment=CUJ5_TARGET_ENV,
    )
    environment["UG_BUDGET_FUTURE_METADATA"] = "future-value"
    environment["UG_BUDGET_CALLER_TOKEN"] = "caller-token"
    environment["UG_BUDGET_CALLER_SECRET"] = "caller-secret"
    with patch.dict(runner.os.environ, environment, clear=True):
        assert runner.main() == 0

    assert mint_calls == [(workspace, "cuj-client", "cuj-secret")]
    assert account_calls == [
        (
            CUJ5_TARGET_ENV["UG_BUDGET_ACCOUNT_HOST"],
            "cuj-client",
            "cuj-secret",
            CUJ5_TARGET_ENV["UG_BUDGET_ACCOUNT_ID"],
        ),
    ]
    pytest_kwargs = _pytest_call(process_calls)
    pytest_environment = pytest_kwargs["env"]
    assert pytest_kwargs["interrupt_grace"] == 180
    assert pytest_environment["UCODE_TEST_WORKSPACE"] == CUJ5_TARGET_ENV["UCODE_TEST_WORKSPACE"]
    assert pytest_environment["DATABRICKS_BEARER"] == "workspace-token"
    assert "DATABRICKS_CLIENT_ID" not in pytest_environment
    assert "DATABRICKS_CLIENT_SECRET" not in pytest_environment
    assert pytest_environment["UG_BUDGET_ACCOUNT_TOKEN"] == "account-token"
    assert {key: pytest_environment[key] for key in CUJ5_TARGET_ENV} == CUJ5_TARGET_ENV
    assert pytest_environment["UG_BUDGET_FUTURE_METADATA"] == "future-value"
    assert "UG_BUDGET_CALLER_TOKEN" not in pytest_environment
    assert "UG_BUDGET_CALLER_SECRET" not in pytest_environment
    assert all(
        "DATABRICKS_CLIENT_ID" not in kwargs.get("env", {})
        and "DATABRICKS_CLIENT_SECRET" not in kwargs.get("env", {})
        and "UG_BUDGET_ACCOUNT_TOKEN" not in kwargs.get("env", {})
        and not any(key.startswith("UG_BUDGET_") for key in kwargs.get("env", {}))
        for command, kwargs in process_calls
        if not ("-m" in command and "pytest" in command)
    )


def test_main_redacts_budget_account_token_errors(tmp_path, monkeypatch):
    args, environment, mint_calls, account_calls, process_calls = _main_setup(
        tmp_path,
        monkeypatch,
        workspace=CUJ5_TARGET_ENV["UCODE_TEST_WORKSPACE"],
        client_id="cuj-client",
        client_secret="cuj-secret",
        target_environment=CUJ5_TARGET_ENV,
    )

    def reject_account_token(account_host, client_id, client_secret, *, account_id):
        account_calls.append((account_host, client_id, client_secret, account_id))
        raise urllib.error.HTTPError(
            account_host,
            401,
            f"account token endpoint rejected: {client_secret}",
            {},
            None,
        )

    monkeypatch.setattr(runner, "mint_account_m2m_token", reject_account_token)
    with patch.dict(runner.os.environ, environment, clear=True):
        assert runner.main() == 1
    report = json.loads((args.output / "versions.json").read_text())
    assert "account token endpoint rejected" in report["error"]
    assert "cuj-secret" not in report["error"]
    assert mint_calls == [(CUJ5_TARGET_ENV["UCODE_TEST_WORKSPACE"], "cuj-client", "cuj-secret")]
    assert account_calls == [
        (
            CUJ5_TARGET_ENV["UG_BUDGET_ACCOUNT_HOST"],
            "cuj-client",
            "cuj-secret",
            CUJ5_TARGET_ENV["UG_BUDGET_ACCOUNT_ID"],
        )
    ]
    assert not any("-m" in command and "pytest" in command for command, _ in process_calls)


def test_main_omits_budget_environment_when_not_provided(tmp_path, monkeypatch):
    args, environment, mint_calls, account_calls, process_calls = _main_setup(
        tmp_path,
        monkeypatch,
        workspace="https://workspace.example",
        client_id="client",
        client_secret="secret",
    )
    with patch.dict(runner.os.environ, environment, clear=True):
        assert runner.main() == 0
    assert mint_calls == [("https://workspace.example", "client", "secret")]
    assert account_calls == []
    pytest_kwargs = _pytest_call(process_calls)
    pytest_environment = pytest_kwargs["env"]
    assert pytest_kwargs["interrupt_grace"] == 15
    assert pytest_environment["UCODE_TEST_WORKSPACE"] == "https://workspace.example"
    assert not any(key.startswith("UG_BUDGET_") for key in pytest_environment)
    assert all(
        not any(key.startswith("UG_BUDGET_") for key in kwargs.get("env", {}))
        for command, kwargs in process_calls
        if not ("-m" in command and "pytest" in command)
    )


def test_budget_client_secret_is_redacted_and_excluded_from_agent_environment(tmp_path):
    with patch.dict(
        "os.environ",
        {
            "DATABRICKS_CLIENT_SECRET": "sensitive-client-secret",
            "UG_BUDGET_ACCOUNT_TOKEN": "sensitive-account-token",
        },
    ):
        environment = clean_environment(tmp_path)
        session = UserSession(tmp_path, tmp_path, Path("ug"), tmp_path / "artifacts")
        assert "DATABRICKS_CLIENT_SECRET" not in environment
        assert "UG_BUDGET_ACCOUNT_TOKEN" not in environment
        assert session.redact(
            "request failed: sensitive-client-secret; token: sensitive-account-token"
        ) == ("request failed: <redacted>; token: <redacted>")
        assert session.redact("request failed: sensitive-account-token") == (
            "request failed: <redacted>"
        )


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
