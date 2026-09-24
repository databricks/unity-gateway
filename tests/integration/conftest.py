"""Standalone fixtures: application state is created only by installed CLI commands."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest
from utils.harness import UserSession
from utils.managed import MANAGED_CONFIGS_PATH, assert_no_managed_config
from utils.terminal import TerminalProcess


def pytest_collection_modifyitems(config, items):
    agents = os.environ.get("UG_INTEGRATION_AGENTS", "claude,codex").split(",")
    selected, deselected = [], []
    for item in items:
        # Fail if someone accidentally invokes this under the unit-test fixtures.
        if "monkeypatch" in item.fixturenames:
            raise pytest.UsageError("Use the integration runner; unit fixtures were inherited.")
        if any(item.get_closest_marker(a) and a not in agents for a in ("claude", "codex")):
            deselected.append(item)
        else:
            selected.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(scope="session")
def installed_binary():
    raw = os.environ.get("UG_INTEGRATION_BIN")
    if not raw or not Path(raw).is_file():
        pytest.fail("Run scripts/run_integration.py to install the version under test.")
    return Path(raw)


@pytest.fixture(scope="session")
def workspace():
    value = os.environ.get("UCODE_TEST_WORKSPACE", "").strip().rstrip("/")
    if not value.startswith("https://") or not os.environ.get("DATABRICKS_BEARER", "").strip():
        pytest.fail("Live integration requires UCODE_TEST_WORKSPACE and DATABRICKS_BEARER.")
    return value


@pytest.fixture
def workspace_bearer(workspace):
    """Supply a token scoped to an explicitly chosen integration workspace."""

    def bearer_for(target_workspace: str) -> str:
        if target_workspace == workspace:
            return os.environ["DATABRICKS_BEARER"]

        client_id = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
        client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            pytest.fail(
                f"Set DATABRICKS_CLIENT_ID and DATABRICKS_CLIENT_SECRET to authenticate "
                f"against {target_workspace}."
            )
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        request = urllib.request.Request(
            f"{target_workspace.rstrip('/')}/oidc/v1/token",
            data=urllib.parse.urlencode(
                {"grant_type": "client_credentials", "scope": "all-apis"}
            ).encode(),
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response).get("access_token", "")
        assert token, f"Workspace {target_workspace} returned no OAuth access token."
        return token

    return bearer_for


@pytest.fixture(scope="session")
def unmanaged_workspace(workspace):
    """Require a real no-config workspace, without injecting or changing its policy."""
    request = urllib.request.Request(
        workspace + MANAGED_CONFIGS_PATH,
        headers={"Authorization": f"Bearer {os.environ['DATABRICKS_BEARER']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        # Like the product, accept NOT_FOUND as no published config. Auth failures
        # and other HTTP errors do not establish the unmanaged prerequisite.
        if error.code != 404:
            raise
        return workspace
    assert_no_managed_config(payload)
    return workspace


@pytest.fixture(scope="session")
def second_workspace(workspace):
    value = os.environ.get("UCODE_TEST_SECOND_WORKSPACE", "").strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not os.environ.get("DATABRICKS_SECOND_BEARER", "").strip()
    ):
        pytest.fail(
            "Workspace-switch CUJs require --second-workspace (or UCODE_TEST_SECOND_WORKSPACE) "
            "and DATABRICKS_SECOND_BEARER for that workspace."
        )
    if parsed.hostname == urlparse(workspace).hostname:
        pytest.fail("Workspace-switch CUJs require two distinct workspace hosts.")
    return value


@pytest.fixture
def session(request, installed_binary):
    # Codex rejects helper installation beneath /tmp. Keep the disposable home
    # under the runner's own directory, never in the developer's agent folders.
    root = Path(os.environ["UG_INTEGRATION_RUN_DIR"])
    case = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
    with (
        tempfile.TemporaryDirectory(prefix="case-", dir=root) as temporary,
        tempfile.TemporaryDirectory(prefix="ug-integration-project-") as project,
    ):
        # Agents walk parent directories for project settings. Keeping cwd out
        # of the checkout prevents its .claude/AGENTS.md from influencing a run.
        user = UserSession(
            Path(temporary), Path(project), installed_binary, root / "artifacts" / case
        )
        try:
            yield user
        finally:
            # Restore machine-level settings through the same public CLI that
            # created them. A later fresh-home test must not inherit this setup.
            if any(
                (user.home / ".ucode" / name).is_file()
                for name in ("state.json", "managed-backups/manifest.json")
            ):
                with TerminalProcess(
                    user, "ug", [str(user.binary), "revert"], "cleanup-revert"
                ) as terminal:
                    terminal.finish()


@pytest.fixture
def live_session(session, workspace):
    for binary in ["databricks", *os.environ["UG_INTEGRATION_AGENTS"].split(",")]:
        if not shutil.which(binary, path=session.env["PATH"]):
            pytest.fail(f"Required integration binary is missing: {binary}")
    session.env["DATABRICKS_BEARER"] = os.environ["DATABRICKS_BEARER"]
    return session


@pytest.fixture(scope="session")
def claude_provider():
    return os.environ["UG_INTEGRATION_CLAUDE_PROVIDER"]


@pytest.fixture(scope="session")
def claude_relayed_provider():
    return os.environ["UG_INTEGRATION_CLAUDE_RELAYED_PROVIDER"]


@pytest.fixture(scope="session")
def claude_oauth_token():
    # Real subscription OAuth token (`claude setup-token`); the relayed launch needs it to run
    # headless. Missing means the launch would fall back to a browser login, so fail rather than
    # silently pass a degraded run — the suite has no capability skips.
    token = os.environ.get("UG_INTEGRATION_CLAUDE_OAUTH_TOKEN", "").strip()
    if not token:
        pytest.fail(
            "Set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) to run the relayed CUJ."
        )
    return token


@pytest.fixture(scope="session")
def claude_provider_model():
    return os.environ["UG_INTEGRATION_CLAUDE_PROVIDER_MODEL"]


@pytest.fixture(scope="session")
def claude_bedrock_allow_all_provider():
    # A Bedrock MPS with allow_all_targets and no declared targets (issue #811). The runner always
    # supplies this (default in run_integration.py); the suite has no capability skips.
    return os.environ["UG_INTEGRATION_CLAUDE_BEDROCK_ALLOW_ALL_PROVIDER"]


@pytest.fixture(scope="session")
def claude_bedrock_allow_all_model():
    # An allow_all service declares no targets, so a launch must name the Bedrock model id itself.
    return os.environ["UG_INTEGRATION_CLAUDE_BEDROCK_ALLOW_ALL_MODEL"]


@pytest.fixture(scope="session")
def codex_provider():
    return os.environ["UG_INTEGRATION_CODEX_PROVIDER"]


@pytest.fixture(scope="session")
def codex_provider_model():
    return os.environ["UG_INTEGRATION_CODEX_PROVIDER_MODEL"]


@pytest.fixture(scope="session")
def parent_schema():
    return os.environ["UG_INTEGRATION_PARENT_SCHEMA"]


@pytest.fixture(scope="session")
def claude_parent_model():
    return os.environ["UG_INTEGRATION_CLAUDE_PARENT_MODEL"]


@pytest.fixture(scope="session")
def codex_parent_model():
    return os.environ["UG_INTEGRATION_CODEX_PARENT_MODEL"]
