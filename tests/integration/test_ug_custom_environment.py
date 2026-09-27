"""Exact custom environments and removal through installed ug and real agent tasks."""

import json
import sys
import uuid
from pathlib import Path

import pytest
from utils.constants import CLAUDE_TEST_MODEL, CODEX_TEST_MODEL
from utils.evidence import agent_sessions, assistant_answers, is_child_session
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
)
from utils.terminal import AgentTerminal


class EnvironmentTask:
    """An ordinary project script reads only the scenario's named test variables."""

    def __init__(self, session, expected, phase):
        self.expected = expected
        self.phase = phase
        self.filename = "inspect-environment.py"
        (session.cwd / self.filename).write_text(
            "import json, os\n"
            f"names = {list(expected)!r}\n"
            "print(json.dumps({name: os.environ.get(name) for name in names}))\n"
        )
        self.prompt = (
            f"Run python3 {self.filename} using a shell tool. "
            "Reply with exactly the JSON it prints, without a code fence. "
            "Do not inspect any other files."
        )

    def completed(self, session, agent):
        for path, records in agent_sessions(session, agent).items():
            if is_child_session(agent, path, records):
                continue
            for answer in assistant_answers(agent, records):
                try:
                    if json.loads(answer) == self.expected:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def assert_completed(self, session, agent):
        session.record(f"{self.phase}-environment-sessions.json", agent_sessions(session, agent))
        assert self.completed(session, agent), (
            "No completed answer matched the exact test environment"
        )


@pytest.mark.installation
def test_ug_and_ucode_reject_reserved_custom_environment_before_setup(session):
    """Scenario: both installed entry points receive file-provided auth overrides.

    Expected: Claude and Codex reject the conflicting name before authentication,
    bootstrap, or state creation, without echoing its value or polluting app-server stdout.
    """
    source = session.cwd / "custom environment.json"
    secret = "not-a-real-token-" + uuid.uuid4().hex
    for agent, entry in (
        ("claude", build_claude_agent_config([CLAUDE_TEST_MODEL])),
        ("codex", build_codex_agent_config(models=[CODEX_TEST_MODEL])),
    ):
        entry["config"]["custom_env"] = {"OAUTH_TOKEN": secret}
        source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
        for executable in ("ug", "ucode"):
            result = session.run(
                agent,
                "--workspace",
                "https://workspace.example.invalid",
                "-f",
                str(source),
                *(["app-server", "--listen", "stdio://"] if agent == "codex" else []),
                binary=session.binary.with_name(executable),
                ok=False,
                timeout=30,
                strip_ansi=False,
            )
            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "OAUTH_TOKEN" in output and "reserved" in output
            assert secret not in output and "Traceback" not in output
            if agent == "codex":
                assert result.stdout == ""
            assert not (session.home / ".ucode").exists()
            assert not (session.home / ".claude").exists()
            assert not (session.home / ".codex").exists()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.claude
def test_ug_claude_custom_environment_updates_and_removes_caller_values(live_session, workspace):
    """Scenario: launch Claude from custom_env, then remove keys still supplied by the caller.

    Expected: real TUI shell tasks return exact empty/newline values initially and
    absent retired keys on the next launch. Unrelated inherited env survives, both
    private/managed settings reconcile, and the caller's input file stays unchanged.
    """
    session = live_session
    custom = {"UG_CUJ_VALUE": uuid.uuid4().hex, "UG_CUJ_EMPTY": "", "UG_CUJ_LINES": " a\nb \n"}
    inherited = uuid.uuid4().hex
    session.env.update({**dict.fromkeys(custom, "stale-parent"), "UG_CUJ_INHERITED": inherited})
    caller = session.cwd / "caller settings.json"
    caller_content = json.dumps({"env": dict.fromkeys(custom, "stale-caller")})
    caller.write_text(caller_content)
    entry = build_claude_agent_config([CLAUDE_TEST_MODEL])
    entry["config"]["custom_env"] = custom
    source = session.cwd / "custom environment.json"
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    command = [
        str(session.binary),
        "claude",
        "--workspace",
        workspace,
        "-f",
        str(source),
        "--",
        "--settings",
        str(caller),
        "--allowedTools",
        "Bash(python3 inspect-environment.py)",
    ]
    task = EnvironmentTask(session, {**custom, "UG_CUJ_INHERITED": inherited}, "initial")
    with AgentTerminal(session, "claude", command, "custom-environment") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    settings = session.home / ".claude/ucode-settings.json"
    managed = Path(
        "/Library/Application Support/ClaudeCode/managed-settings.json"
        if sys.platform == "darwin"
        else "/etc/claude-code/managed-settings.json"
    )
    for path in (settings, managed):
        assert all(
            json.loads(path.read_text())["env"][key] == value for key, value in custom.items()
        )
    assert "claude_custom_env" not in session.workspace_state()

    entry["config"]["custom_env"] = {}
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    task = EnvironmentTask(
        session, {**dict.fromkeys(custom), "UG_CUJ_INHERITED": inherited}, "removed"
    )
    with AgentTerminal(session, "claude", command, "removed-environment") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    for path in (settings, managed):
        assert not set(custom).intersection(json.loads(path.read_text()).get("env", {}))
    assert caller.read_text() == caller_content
    assert not (session.home / ".ucode/managed-config.json").exists()
    session.assert_not_routed()


@pytest.mark.live
@pytest.mark.tui
@pytest.mark.codex
def test_ug_codex_custom_environment_reaches_tools_and_scrubs_inherited_values(
    live_session, workspace
):
    """Scenario: launch Codex from custom_env, then omit keys still set in the parent.

    Expected: real TUI shell tasks observe exact strings initially and absent retired
    keys after the next launch, while unrelated inheritance remains. This covers
    ug-launched Codex and its ordinary children, not independent desktop processes.
    """
    session = live_session
    custom = {"UG_CUJ_VALUE": uuid.uuid4().hex, "UG_CUJ_EMPTY": "", "UG_CUJ_LINES": " a\nb \n"}
    inherited = uuid.uuid4().hex
    session.env.update({**dict.fromkeys(custom, "stale-parent"), "UG_CUJ_INHERITED": inherited})
    entry = build_codex_agent_config(models=[CODEX_TEST_MODEL])
    entry["config"]["custom_env"] = custom
    source = session.cwd / "custom environment.json"
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    command = [str(session.binary), "codex", "--workspace", workspace, "-f", str(source)]
    task = EnvironmentTask(session, {**custom, "UG_CUJ_INHERITED": inherited}, "initial")
    with AgentTerminal(session, "codex", command, "custom-environment") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    assert "codex_custom_env" not in session.workspace_state()

    entry["config"].pop("custom_env")
    source.write_text(json.dumps(build_coding_agent_config(entry["agent"], entry)))
    task = EnvironmentTask(
        session, {**dict.fromkeys(custom), "UG_CUJ_INHERITED": inherited}, "removed"
    )
    with AgentTerminal(session, "codex", command, "removed-environment") as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
    assert not (session.home / ".ucode/managed-config.json").exists()
    session.assert_not_routed()
