"""CUJs: which files `ug configure` writes for each scope, and what a later launch changes.

The default writes the per-user settings and the OS-managed file, `--user` and
`UG_SKIP_OS_SETTINGS` write only the per-user settings, and `--global` writes only the
OS-managed file. A launch never changes the OS-managed file.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask, fresh_id

pytestmark = [pytest.mark.live]

SCOPES = ["default", "user", "skip_os_settings", "global"]
CLAUDE_MANAGED = Path(
    "/Library/Application Support/ClaudeCode/managed-settings.json"
    if sys.platform == "darwin"
    else "/etc/claude-code/managed-settings.json"
)
CODEX_MANAGED = Path("/etc/codex/managed_config.toml")


@pytest.fixture(scope="module")
def shared_ug(installed_binary):
    """The version under test installed outside every home folder, as `--global` requires.

    Like `scripts/mdm`, the Python goes in the shared location too. The runner's interpreter
    is copied there when it lives in a home folder, and the runner's frozen package set is
    installed into a new environment.
    """
    sudo = shutil.which("sudo")
    assert sudo, "The --global journey needs sudo to create a shared installation."
    root = Path("/opt") / f"ug-integration-{fresh_id()}"
    subprocess.run(
        [sudo, "-n", "install", "-d", "-m", "0755", "-o", str(os.getuid()), str(root)],
        check=True,
        timeout=30,
    )
    try:
        base = (installed_binary.parent / "python").resolve()
        if any(base.is_relative_to(home) for home in (Path("/home"), Path("/Users"))):
            shutil.copytree(base.parent.parent, root / "python", symlinks=True)
            base = root / "python" / "bin" / base.name
        venv = root / "venv"
        subprocess.run([str(base), "-m", "venv", str(venv)], check=True, timeout=120)
        frozen = Path(os.environ["UG_INTEGRATION_RUN_DIR"]) / "installed.txt"
        subprocess.run(
            [
                str(venv / "bin/python"),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--index-url",
                os.environ["UV_DEFAULT_INDEX"],
                "-r",
                str(frozen),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=600,
        )
        binary = venv / "bin" / installed_binary.name
        versions = [
            subprocess.run(
                [str(path), "--version"], capture_output=True, text=True, check=True, timeout=60
            ).stdout
            for path in (installed_binary, binary)
        ]
        assert versions[0] == versions[1], versions
        yield binary
    finally:
        subprocess.run([sudo, "-n", "rm", "-rf", str(root)], check=False, timeout=60)


def _snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _check_scope(request, session, workspace, scope, *, agent, launch):
    managed = CLAUDE_MANAGED if agent == "claude" else CODEX_MANAGED
    ug_file = session.home / (
        ".claude/ucode-settings.json" if agent == "claude" else ".codex/ucode.config.toml"
    )
    user_file = session.home / (
        ".claude/settings.json" if agent == "claude" else ".codex/config.toml"
    )
    original = (
        '{"env":{"UG_USER_SETTING":"keep"}}\n'
        if agent == "claude"
        else "# user comment\n[notice]\nhide_rate_limit_model_nudge = true\n"
    )
    user_file.parent.mkdir(parents=True)
    user_file.write_text(original)
    assert not managed.exists(), f"{managed} must not exist before the journey"

    flags = []
    if scope == "user":
        flags = ["--user"]
    elif scope == "skip_os_settings":
        session.env["UG_SKIP_OS_SETTINGS"] = "1"
    elif scope == "global":
        flags = ["--global"]
        session.binary = request.getfixturevalue("shared_ug")
        session.env["PATH"] = os.pathsep.join([str(session.binary.parent), session.env["PATH"]])
    writes_managed = scope in ("default", "global")

    session.run(
        "configure",
        *flags,
        "--agents",
        agent,
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )

    assert managed.exists() == writes_managed
    if writes_managed:
        if agent == "claude":
            settings = json.loads(managed.read_text())
            assert settings["env"]["ANTHROPIC_BASE_URL"] == f"{workspace}/ai-gateway/anthropic"
            helper = settings["apiKeyHelper"]
        else:
            provider = tomllib.loads(managed.read_text())["model_providers"]["Databricks"]
            assert provider["base_url"] == f"{workspace}/ai-gateway/codex/v1"
            helper = provider["auth"]["command"]
        # Every user's agents run the helper, so --global must point it at the shared install.
        assert str(session.binary) in helper, helper
    assert ug_file.exists() == (scope != "global")
    if scope != "global":
        assert workspace in ug_file.read_text()
    assert session.state().get("config_scope", "global") == (
        "user" if scope == "user" else "global"
    )

    def assert_user_file_kept():
        if scope == "default":
            # The default may add ug's Codex app settings; the user's own keys stay.
            loads = json.loads if agent == "claude" else tomllib.loads
            current = loads(user_file.read_text())
            assert all(current.get(key) == value for key, value in loads(original).items())
        else:
            assert user_file.read_text() == original

    assert_user_file_kept()

    managed_before = _snapshot(managed)
    task = FileTask(session)
    task.assert_headless_answer(agent, session.run(*launch(task), timeout=180))
    assert _snapshot(managed) == managed_before, "A launch changed the OS-managed file"
    # A launch after --global writes that user's per-user settings.
    assert ug_file.exists()
    assert_user_file_kept()


@pytest.mark.claude
@pytest.mark.parametrize("scope", SCOPES)
def test_ug_configure_scope_claude(request, live_session, workspace, scope):
    """Scenario: configure Claude in one scope over a user-owned settings file, then launch.

    Expected: default writes `~/.claude/ucode-settings.json` and the managed file; `--user` and
    `UG_SKIP_OS_SETTINGS` write only the per-user file; `--global` writes only the managed file,
    whose `apiKeyHelper` is the shared installation. `~/.claude/settings.json` is unchanged,
    the saved scope is `user` only after `--user`, and the launch answers through the gateway
    without changing the managed file.
    """
    _check_scope(
        request,
        live_session,
        workspace,
        scope,
        agent="claude",
        launch=lambda task: (
            "claude",
            "--",
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
        ),
    )


@pytest.mark.codex
@pytest.mark.parametrize("scope", SCOPES)
def test_ug_configure_scope_codex(request, live_session, workspace, scope):
    """Scenario: configure Codex in one scope over a user-owned config.toml, then launch.

    Expected: default writes `~/.codex/ucode.config.toml` and the managed file; `--user` and
    `UG_SKIP_OS_SETTINGS` write only the per-user file; `--global` writes only the managed file,
    whose auth command is the shared installation. `~/.codex/config.toml` is unchanged
    (default keeps the user's keys), the saved scope is `user` only after `--user`, and the
    launch answers through the gateway without changing the managed file.
    """
    _check_scope(
        request,
        live_session,
        workspace,
        scope,
        agent="codex",
        launch=lambda task: (
            "codex",
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--model",
            CODEX_TEST_MODEL,
            task.prompt,
        ),
    )
