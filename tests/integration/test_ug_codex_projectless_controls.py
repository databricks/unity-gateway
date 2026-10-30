"""Compare the existing Windows projectless permission boundary with the candidate."""

from __future__ import annotations

import json
import os

import pytest
from test_ug_codex_v2_routing import _seed_user_owned_native_codex_settings
from utils.evidence import FileTask, agent_sessions, assistant_answers
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.tui, pytest.mark.codex]


def test_ug_codex_projectless_controls_respect_native_permissions(
    live_session, workspace, tmp_path
):
    """Scenario: launch stock Codex in a Windows folder without a project or explicit
    approval settings, then run the public session toggle using default permissions.

    Expected: a real file task completes, the private session controls remain
    protected, the denied toggle leaves routing unchanged, and native permission
    settings remain intact. Run the same test against main and the candidate.
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
    summary = {"passed": False, "version": session.run("--version").stdout.strip()}
    command = '& "$env:UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing'

    def records():
        return [row for rows in agent_sessions(session, "codex").values() for row in rows]

    try:
        with AgentTerminal(
            session, "codex", [str(session.binary), "codex"], "projectless-controls"
        ) as tui:
            tui.boot()
            task = FileTask(session)
            tui.submit(task.prompt + " Do this directly without delegation.")
            tui.wait_for_task(task, timeout=180)
            (controls,) = tmp_path.glob("ug-session-env-*/env.json")
            before = controls.read_bytes()
            assert json.loads(before) == {}
            before_answers = len(assistant_answers("codex", records()))
            tui.submit(
                "Run exactly this PowerShell command with default sandbox permissions and "
                "report its output. Do not request broader access, change file permissions, "
                "or edit the controls yourself:\n" + command
            )

            def completed(_):
                return len(assistant_answers("codex", records())) > before_answers

            tui.wait_for(completed, "the denied public toggle result", timeout=120)
            rows = records()
            executions = [
                row["payload"]["item"]
                for row in rows
                if row.get("type") == "event_msg"
                and row.get("payload", {}).get("type") == "item_completed"
                and row["payload"].get("item", {}).get("type") == "CommandExecution"
                and row["payload"]["item"].get("command", [""])[-1] == command
            ]
            assert len(executions) == 1, executions
            execution = executions[0]
            assert execution["exit_code"] == 1, execution
            assert "Smart Router session controls" in execution["aggregated_output"]
            assert controls.read_bytes() == before
            contexts = [row["payload"] for row in rows if row.get("type") == "turn_context"]
            assert contexts
            assert all(
                context["approval_policy"]["granular"]["sandbox_approval"] is False
                and context["sandbox_policy"]["type"] == "workspace-write"
                for context in contexts
            )
            tui.exit_normally()
            summary.update(
                passed=True,
                toggle_exit_code=execution["exit_code"],
                toggle_output=execution["aggregated_output"],
                controls_unchanged=True,
                permission_settings_preserved=True,
                ordinary_task_completed=True,
                normal_exit=True,
            )
    finally:
        session.record("projectless-controls-summary.json", summary)
        session.record("projectless-controls-sessions.json", agent_sessions(session, "codex"))
