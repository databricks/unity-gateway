"""Diagnose the native Windows sandbox boundary using real session controls."""

from __future__ import annotations

import json
import os

import pytest
from test_ug_codex_v2_routing import _seed_user_owned_native_codex_settings
from test_ug_smart_routing_hooks import _codex_command_approval_pending
from utils.evidence import agent_sessions, assistant_answers, tool_outputs
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


def test_ug_codex_windows_session_controls(live_session, workspace, tmp_path):
    """Scenario: read UG's real session controls inside/outside the Windows sandbox,
    then request native approval for the exact public off/on commands.

    Expected: the read-only sandbox reports PermissionError without changing the
    controls; each explicitly approved CLI command succeeds. This diagnostic is
    selected only on Windows and does not claim default-skill or delegation coverage.
    """
    assert os.name == "nt"
    session = live_session
    session.env.update(
        ENABLE_SMART_ROUTING_V2="1",
        ENABLE_SMART_ROUTING_SUBAGENT_ONLY="1",
        TMPDIR=str(tmp_path),
    )
    _seed_user_owned_native_codex_settings(session)
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
    summary = {"passed": False}
    python = session.binary.with_name("python.exe")
    assert python.is_file()
    probe = session.cwd / "read_controls.py"
    probe.write_text(
        "import json, pathlib, sys\n"
        "try:\n"
        "    value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "except OSError as error:\n"
        "    print(json.dumps({'type': type(error).__name__, 'errno': error.errno, "
        "'winerror': getattr(error, 'winerror', None), 'message': str(error)}))\n"
        "    raise SystemExit(13)\n"
        "print(json.dumps({'value': value}))\n",
        encoding="utf-8",
    )
    try:
        with AgentTerminal(
            session, "codex", [str(session.binary), "codex"], "windows-session-controls"
        ) as tui:
            tui.boot()
            (controls,) = tmp_path.glob("ug-session-env-*/env.json")
            assert json.loads(controls.read_text()) == {}
            outside = session.run(str(probe), str(controls), binary=python)
            assert json.loads(outside.stdout) == {"value": {}}
            restricted = session.run(
                "-c",
                'sandbox_mode="read-only"',
                "sandbox",
                "--",
                str(python),
                str(probe),
                str(controls),
                binary=session.which("codex"),
                ok=False,
            )
            summary["outside_read"] = json.loads(outside.stdout)
            assert restricted.returncode == 13, restricted
            summary["sandbox_read"] = json.loads(restricted.stdout)
            assert summary["sandbox_read"]["type"] == "PermissionError"
            assert json.loads(controls.read_text()) == {}
            session.run(str(controls.parent), binary=session.which("icacls"))
            session.env["UCODE_SESSION_ENV_FILE"] = str(controls)
            try:
                denied_toggle = session.run(
                    "-c",
                    'sandbox_mode="read-only"',
                    "sandbox",
                    "--",
                    str(python),
                    "-m",
                    "ucode.cli",
                    "codex",
                    "--disable-smart-routing",
                    binary=session.which("codex"),
                    ok=False,
                )
            finally:
                session.env.pop("UCODE_SESSION_ENV_FILE")
            assert denied_toggle.returncode == 1, denied_toggle
            assert "are invalid" in denied_toggle.stdout + denied_toggle.stderr
            assert json.loads(controls.read_text()) == {}
            summary["sandbox_toggle"] = {"returncode": denied_toggle.returncode}

            def toggle(enabled):
                state = "on" if enabled else "off"
                flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"
                command = f'& "$env:UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex {flag}'
                confirmation = f"Smart Router is {state} for this session"
                expected = (
                    {}
                    if enabled
                    else {
                        "ENABLE_SMART_ROUTING_V2": "0",
                        "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
                        "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
                    }
                )
                before_answers = sum(
                    len(assistant_answers("codex", rows))
                    for rows in agent_sessions(session, "codex").values()
                )
                tui.submit(
                    "The session controls directory is outside this sandbox. Request native "
                    "approval for exactly the following PowerShell command using "
                    'sandbox_permissions="require_escalated". Do not change permissions or '
                    f"edit files yourself. Run the approved command and report its result:\n{command}"
                )
                approved = False

                def completed(screen):
                    nonlocal approved
                    if not approved and _codex_command_approval_pending(screen, command):
                        tui.send("\r", f"approve only the session routing {state} command")
                        approved = True
                        return False
                    records = [
                        row for rows in agent_sessions(session, "codex").values() for row in rows
                    ]
                    return (
                        approved
                        and json.loads(controls.read_text()) == expected
                        and any(confirmation in value for value in tool_outputs("codex", records))
                        and len(assistant_answers("codex", records)) > before_answers
                    )

                tui.wait_for(completed, f"the approved routing {state} command", timeout=120)
                summary[state] = {"native_approval": approved, "controls": expected}

            for enabled in [False, True]:
                toggle(enabled)
            tui.exit_normally()
            summary["passed"] = True
    finally:
        session.record("windows-session-controls-summary.json", summary)
        session.record("windows-session-controls-sessions.json", agent_sessions(session, "codex"))
