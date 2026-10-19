"""CUJs for Copilot's ug sign-in over a session, and for undoing its ug setup."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import FileTask, assert_no_bearer_on_disk, fresh_id
from utils.terminal import TerminalProcess

pytestmark = [pytest.mark.live, pytest.mark.copilot]

PAT_BEARER = Path(__file__).parent / "utils" / "pat_bearer.py"
# v0.2.0, the last release that saved the Databricks token in ~/.copilot/ucode.env.
TOKEN_SAVING_UG = (
    "unity-gateway @ git+https://github.com/databricks/unity-gateway"
    "@cb7bd8c54800ac59b9701ea0ac9f561c83d07bf7"
)


def _configure(session, workspace):
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )


def _pat_sign_in(session, workspace):
    """Switch the session to ug's bearer-command sign-in, minting short-lived PATs.

    Returns the state dir and the shell step Copilot runs to revoke or sign out; the step
    uses only paths under the session's working directory."""
    state = session.cwd / f"auth-{fresh_id()}"
    state.mkdir()
    shutil.copy(PAT_BEARER, state / PAT_BEARER.name)
    argv = [sys.executable, "-I", str(state / PAT_BEARER.name), "mint", workspace, str(state)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    session.env["UG_INTEGRATION_ISSUER_BEARER"] = session.env.pop("DATABRICKS_BEARER")
    session.env["DATABRICKS_BEARER_COMMAND"] = command
    python = "python" if os.name == "nt" else "python3"
    relative = f"{state.name}/{PAT_BEARER.name}"

    def step(mode):
        return f"{python} {relative} {mode} {workspace} {state.name}"

    return state, step


def _delete_minted(session, workspace, state):
    subprocess.run(
        [sys.executable, "-I", str(state / PAT_BEARER.name), "cleanup", workspace, str(state)],
        env=session.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )


def _install_token_saving_ug(session) -> Path:
    uv = shutil.which("uv", path=session.env["PATH"])
    assert uv, "Installing the older ug release needs uv."
    venv = session.home.parent / "ug-0.2.0"
    for command in (
        [uv, "venv", "--python", "3.12", str(venv)],
        [uv, "pip", "install", "--python", str(venv), TOKEN_SAVING_UG],
    ):
        subprocess.run(
            command,
            env=session.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            check=True,
        )
    return venv / ("Scripts/ug.exe" if os.name == "nt" else "bin/ug")


def _minted_count(state):
    return len((state / "minted").read_text(encoding="utf-8").split())


def test_ug_copilot_refreshes_a_revoked_token_mid_session(live_session, workspace):
    """Scenario: sign in through a bearer command, then revoke the session's token mid-task.

    The headless Claude task first runs a shell step that deletes every PAT minted so far,
    including the one ug's proxy holds, then reads the fixture.

    Expected: the gateway rejects the revoked token, ug mints a new one without restarting
    Copilot, and Copilot's completed answer contains the fixture's value. Limits: needs the
    e2e principal to create PATs; a gateway that still honors a just-deleted PAT fails the
    mint-after-revoke assertion.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    state, step = _pat_sign_in(session, workspace)
    session.record("copilot-model.json", {"model": CLAUDE_TEST_MODEL})
    prompt = f"First run this shell command exactly once: {step('revoke')} Then: {task.prompt}"

    try:
        result = session.run(
            "copilot",
            "--model",
            CLAUDE_TEST_MODEL,
            "-p",
            prompt,
            *COPILOT_HEADLESS_FLAGS,
            timeout=240,
        )

        task.assert_headless_answer("copilot", result)
        revoked = int((state / "revoked").read_text(encoding="utf-8"))
        session.record(
            "copilot-token-mints.json", {"revoked": revoked, "minted": _minted_count(state)}
        )
        assert _minted_count(state) > revoked, "No token was minted after the revocation"
    finally:
        _delete_minted(session, workspace, state)


def test_ug_copilot_reports_expired_sign_in(live_session, workspace):
    """Scenario: sign in through a bearer command, then sign out and revoke mid-task.

    The headless Claude task first runs a shell step that makes the bearer command fail
    and deletes every PAT minted so far, then reads the fixture.

    Expected: ug cannot mint a replacement, so Copilot's output shows ug's sign-in-expired
    message with the `databricks auth login` fix, and the failure is logged to
    ~/.ucode/logs/copilot-proxy.log instead of Copilot's terminal.
    """
    session = live_session
    task = FileTask(session)
    _configure(session, workspace)
    state, step = _pat_sign_in(session, workspace)
    session.record("copilot-model.json", {"model": CLAUDE_TEST_MODEL})
    prompt = f"First run this shell command exactly once: {step('sign-out')} Then: {task.prompt}"

    try:
        result = session.run(
            "copilot",
            "--model",
            CLAUDE_TEST_MODEL,
            "-p",
            prompt,
            *COPILOT_HEADLESS_FLAGS,
            timeout=240,
            ok=False,
        )

        assert (state / "revoked").exists(), "Copilot never ran the sign-out step"
        output = result.stdout + result.stderr
        assert "Databricks sign-in expired" in output, output
        assert "databricks auth login" in output, output
        log = (session.home / ".ucode/logs/copilot-proxy.log").read_text(encoding="utf-8")
        assert "token refresh failed" in log
        assert "token refresh failed" not in output
    finally:
        _delete_minted(session, workspace, state)


def test_ug_configure_copilot_repeat_and_revert(live_session, workspace):
    """Scenario: configure Copilot twice over a user-owned MCP config, run a task, then revert.

    Expected: repeat setup stays usable, a headless GPT task completes, no file under the
    session home holds the bearer, revert removes ug's generated Copilot env file and leaves
    the user's file byte-identical, and a repeated revert succeeds.
    """
    session = live_session
    task = FileTask(session)
    # Ordinary pre-existing user settings, not manufactured gateway state.
    user_path = session.home / ".copilot/mcp-config.json"
    original = json.dumps({"mcpServers": {}}, indent=2) + "\n"
    user_path.parent.mkdir(parents=True)
    user_path.write_text(original, encoding="utf-8")

    _configure(session, workspace)
    first = session.workspace_state()
    assert "copilot" in first["available_tools"]
    _configure(session, workspace)
    assert session.workspace_state()["available_tools"] == first["available_tools"]
    assert_no_bearer_on_disk(session)
    session.record("copilot-model.json", {"model": CODEX_TEST_MODEL})

    result = session.run(
        "copilot",
        "--model",
        CODEX_TEST_MODEL,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )
    task.assert_headless_answer("copilot", result)
    assert_no_bearer_on_disk(session)

    with TerminalProcess(session, "ug", [str(session.binary), "revert"], "revert") as terminal:
        terminal.finish()
    assert user_path.read_text(encoding="utf-8") == original
    assert not (session.home / ".copilot/ucode.env").exists()
    assert "Not Configured" in session.run("status").stdout
    assert_no_bearer_on_disk(session)
    with TerminalProcess(
        session, "ug", [str(session.binary), "revert"], "repeat-revert"
    ) as terminal:
        terminal.finish()


def test_ug_copilot_upgrade_scrubs_token_saved_by_older_ug(live_session, workspace):
    """Scenario: configure Copilot with ug v0.2.0, upgrade to this ug, configure, use, revert.

    Expected: v0.2.0 leaves the bearer in ~/.copilot/ucode.env (the scenario's input);
    this ug's configure removes it so no file under the session home holds the bearer,
    a headless GPT task completes, and revert leaves no token and no generated env file.
    """
    session = live_session
    task = FileTask(session)
    token_saving_ug = _install_token_saving_ug(session)
    env_file = session.home / ".copilot/ucode.env"

    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        binary=token_saving_ug,
    )
    assert session.env["DATABRICKS_BEARER"] in env_file.read_text(encoding="utf-8"), (
        "ug v0.2.0 no longer saves the bearer, so this journey has no legacy token to scrub"
    )

    _configure(session, workspace)
    assert_no_bearer_on_disk(session)
    session.record("copilot-model.json", {"model": CODEX_TEST_MODEL})
    result = session.run(
        "copilot",
        "--model",
        CODEX_TEST_MODEL,
        "-p",
        task.prompt,
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )
    task.assert_headless_answer("copilot", result)

    with TerminalProcess(session, "ug", [str(session.binary), "revert"], "revert") as terminal:
        terminal.finish()
    assert not env_file.exists()
    assert "Not Configured" in session.run("status").stdout
    assert_no_bearer_on_disk(session)
