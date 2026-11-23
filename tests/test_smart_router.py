"""Behavioral coverage for session-local Smart Router controls."""

import errno
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ucode import cli, config_io, skills
from ucode.skills import SMART_ROUTER_SKILL
from ucode.smart_routing import session_env, v2

runner = CliRunner()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_orchestrator_hook_activates_in_fresh_process(tmp_path, agent):
    controls = tmp_path / "controls.json"
    controls.write_text("{}")
    result = subprocess.run(
        [sys.executable, "-m", "ucode.smart_routing.orchestrator", "--agent", agent],
        input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
        env={
            **os.environ,
            "SMART_ROUTER_CONFIG_VERSION": "subagent_orch_v0",
            session_env.SESSION_ENV_VAR: str(controls),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "Smart Router Orchestrator is on for this session." in output["additionalContext"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_subagent_audit_mismatch_is_silent(monkeypatch, caplog, agent):
    caplog.set_level("DEBUG", logger="ucode.cli")
    audit = Mock(
        return_value={
            "matches_router_decision": False,
            "requested_model": "requested",
            "model": "launched",
        }
    )
    monkeypatch.setattr(f"ucode.smart_routing.{agent}_routing.record_subagent_start", audit)
    result = runner.invoke(
        cli.app,
        [f"{agent}-router-hook", "record-subagent"],
        input="{}",
        env={"ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1"},
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == result.stderr == ""
    audit.assert_called_once_with({})
    assert "router requested requested" in caplog.text
    assert "started launched" in caplog.text


WHEN_ON = {
    "ENABLE_SMART_ROUTING_V2": "0",
    "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "1",
    "ENABLE_SMART_ROUTER_ORCHESTRATOR": "1",
}


def _on_demand_session(tmp_path) -> tuple[Path, Path]:
    controls = tmp_path / "env.json"
    return controls, session_env.start_on_demand(controls, WHEN_ON)


def _on_demand_state(on_demand: Path) -> dict:
    return json.loads(on_demand.read_text())


def test_on_demand_session_turns_on_the_routing_it_recorded(tmp_path, monkeypatch):
    controls, on_demand = _on_demand_session(tmp_path)
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    assert on_demand.name == "smart-routing-on-demand.json"
    assert json.loads(controls.read_text()) == dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
    assert session_env.routing_never_turned_on()

    for flag, state, expected in [
        ("--enable-smart-routing", "on", WHEN_ON),
        ("--disable-smart-routing", "off", dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")),
        ("--enable-smart-routing", "on", WHEN_ON),
    ]:
        result = runner.invoke(cli.app, ["claude", flag])
        assert result.exit_code == 0, result.output
        assert f"Smart Router is {state} for this session" in result.output
        assert json.loads(controls.read_text()) == expected
        assert _on_demand_state(on_demand) == {"when_on": WHEN_ON, "ever_on": True}
        assert not session_env.routing_never_turned_on()


def test_a_session_routed_at_launch_turns_back_on_to_its_launch_routing(tmp_path, monkeypatch):
    controls = tmp_path / "env.json"
    controls.write_text(json.dumps(dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")))
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))

    assert session_env.set_session_routing(True)
    assert json.loads(controls.read_text()) == {}
    assert not session_env.on_demand_path(controls).exists()
    assert not session_env.routing_never_turned_on()


@pytest.mark.parametrize("content", ["not json", "[]", '{"when_on": {}}', '{"ever_on": false}'])
def test_an_invalid_on_demand_file_blocks_turning_routing_on(tmp_path, monkeypatch, content):
    controls, on_demand = _on_demand_session(tmp_path)
    on_demand.write_text(content)
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))

    with pytest.raises(RuntimeError, match="are invalid"):
        session_env.set_session_routing(True)
    assert json.loads(controls.read_text()) == dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
    assert not session_env.routing_never_turned_on()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_orchestrator_hook_says_nothing_until_the_user_turns_routing_on(tmp_path, agent):
    controls, _ = _on_demand_session(tmp_path)

    def hook_context() -> str | None:
        result = subprocess.run(
            [sys.executable, "-m", "ucode.smart_routing.orchestrator", "--agent", agent],
            input=json.dumps({"hook_event_name": "UserPromptSubmit"}),
            # The launch environment keeps routing off; only the session controls turn it on.
            env={
                **os.environ,
                **dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0"),
                session_env.SESSION_ENV_VAR: str(controls),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return (
            json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            if result.stdout
            else None
        )

    assert hook_context() is None
    session_env.set_session_routing(True, env={session_env.SESSION_ENV_VAR: str(controls)})
    assert "Smart Router Orchestrator is on for this session." in hook_context()
    session_env.set_session_routing(False, env={session_env.SESSION_ENV_VAR: str(controls)})
    assert hook_context().startswith("Smart Router Orchestrator is off for this session.")


@pytest.fixture(autouse=True)
def _checkout_skill_bundle(monkeypatch):
    # An editable Python install can still contain an older wheel's copied skill resources.
    monkeypatch.setattr(skills, "_skills_source", lambda: Path(__file__).parents[1] / "skills")
    monkeypatch.setattr(session_env, "CONTROLS_READ_RETRY_SECONDS", 0)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_smart_routed_session_installs_skill(tmp_path, monkeypatch, agent):
    monkeypatch.delenv(session_env.SESSION_ENV_VAR, raising=False)
    monkeypatch.setenv("UCODE_SMART_ROUTER_PYTHON", "/older/install/python")

    session_path = v2._prepare_smart_router_session(agent)

    home = config_io.APP_DIR.parent
    assert home.joinpath(f".{agent}/skills/{SMART_ROUTER_SKILL}/SKILL.md").is_file()
    assert not home.joinpath(f".agents/skills/{SMART_ROUTER_SKILL}").exists()
    assert session_path == Path(os.environ[session_env.SESSION_ENV_VAR])
    assert Path(os.environ[session_env.SESSION_ENV_VAR]).is_file()
    assert os.environ["UCODE_SMART_ROUTER_PYTHON"] == sys.executable


@pytest.mark.skipif(os.name == "nt", reason="Exercises the skill's POSIX shell commands")
def test_claude_skill_toggles_with_launch_installation_despite_shadowed_path(tmp_path, monkeypatch):
    agent = "claude"
    # Keep the venv path (including spaces), not its resolved system Python symlink.
    installation = tmp_path / "launch installation"
    installation.symlink_to(sys.prefix, target_is_directory=True)
    monkeypatch.setattr(sys, "executable", str(installation / "bin" / "python"))
    shadow = tmp_path / "other-installation"
    shadow.mkdir()
    other_ug = shadow / "ug"
    other_ug.write_text("#!/bin/sh\necho 'wrong ug installation' >&2\nexit 97\n")
    other_ug.chmod(0o755)
    monkeypatch.setenv("PATH", str(shadow) + os.pathsep + os.environ.get("PATH", ""))

    session_path = v2._prepare_smart_router_session(agent)
    skill = config_io.APP_DIR.parent / f".{agent}/skills/{SMART_ROUTER_SKILL}/SKILL.md"
    commands = re.findall(r"`([^`\n]*--(?:enable|disable)-smart-routing)`", skill.read_text())

    for action in ("disable", "enable"):
        command = next(cmd for cmd in commands if f" {agent} --{action}-" in cmd)
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=tmp_path,
            env={**os.environ, "NO_COLOR": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Smart Router is {'off' if action == 'disable' else 'on'}" in result.stdout
        expected = dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0") if action == "disable" else {}
        assert json.loads(session_path.read_text()) == expected


@pytest.mark.parametrize(
    "write_step,error_number",
    [
        ("mkstemp", errno.EACCES),
        ("mkstemp", errno.EROFS),
        ("mkdir", errno.EACCES),
        ("replace", errno.EPERM),
        ("replace", errno.EROFS),
        ("mkstemp", errno.ENOSPC),
        ("replace", errno.ENOSPC),
    ],
)
def test_launcher_flags_control_routing_hook(tmp_path, monkeypatch, write_step, error_number):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    env = {
        session_env.SESSION_ENV_VAR: str(session_file),
        v2.ENABLE_SMART_ROUTING_ENV_VAR: "0",
        v2.ENABLE_SUBAGENT_ROUTING_ENV_VAR: "1",
        "DATABRICKS_BEARER": "token",
    }
    route = Mock(return_value=None)
    monkeypatch.setattr("ucode.smart_routing.codex_routing.route_pre_tool_use", route)
    hook_args = [
        "codex-router-hook",
        "route-subagent",
        "--host",
        "https://example.com",
        "--model",
        "system.ai.gpt-5-6-sol",
    ]
    payload = '{"tool_name":"collaboration.spawn_agent","tool_input":{"message":"fix it"}}'

    before = session_file.read_bytes()
    with monkeypatch.context() as denied:
        # Model a sandbox-denied read while leaving the actual controls available for comparison.
        denied.setattr(session_env, "_read", Mock(side_effect=PermissionError("sandbox denied")))
        for flag in ("--disable-smart-routing", "--enable-smart-routing"):
            result = runner.invoke(cli.app, ["codex", flag], env=env)
            assert result.exit_code == 1
            assert "Request native command approval" in " ".join(result.output.split())
            assert "Smart Router is" not in result.output
            assert session_file.read_bytes() == before

    with monkeypatch.context() as denied:
        # Inject filesystem failures at the raw and wrapped atomic-write boundaries.
        target = {"mkstemp": config_io.tempfile, "mkdir": Path, "replace": config_io.os}[write_step]
        original = getattr(target, write_step)

        def fail_session_write(*args, **kwargs):
            path = (
                kwargs["dir"]
                if write_step == "mkstemp"
                else args[-1 if write_step == "replace" else 0]
            )
            if Path(path) in (session_file, session_file.parent):
                raise OSError(error_number, os.strerror(error_number))
            return original(*args, **kwargs)

        denied.setattr(target, write_step, fail_session_write)
        for flag in ("--disable-smart-routing", "--enable-smart-routing"):
            current = (
                {}
                if flag == "--disable-smart-routing"
                else dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
            )
            session_file.write_text(json.dumps(current))
            before = session_file.read_bytes()
            result = runner.invoke(cli.app, ["codex", flag], env=env)
            assert result.exit_code == 1
            output = " ".join(result.output.split())
            assert "writing Smart Router session controls" in output
            assert ("Request native command approval" in output) == (error_number != errno.ENOSPC)
            if error_number == errno.ENOSPC:
                assert os.strerror(errno.ENOSPC) in output
            assert "Smart Router is" not in output
            assert isinstance(result.exception, SystemExit)
            assert session_file.read_bytes() == before
            assert not list(session_file.parent.glob(".env.json.*.tmp"))

    session_file.write_text("{}")
    assert runner.invoke(cli.app, ["codex", "--disable-smart-routing"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_not_called()

    assert runner.invoke(cli.app, ["codex", "--enable-smart-routing"], env=env).exit_code == 0
    assert runner.invoke(cli.app, hook_args, input=payload, env=env).exit_code == 0
    route.assert_called_once()


@pytest.mark.parametrize(
    "launch_mode,reason,turned_off",
    [
        ("omni", None, True),
        (None, None, False),
        ("direct", "too short", False),
        (None, "the router picked a weak model", True),
    ],
)
def test_omni_launches_turn_routing_off_without_a_reason(tmp_path, launch_mode, reason, turned_off):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    env = {
        session_env.SESSION_ENV_VAR: str(session_file),
        "ISAAC_SMART_ROUTER_FEEDBACK": "1",
        "ISAAC_LAUNCH_MODE": launch_mode,
    }
    args = ["claude", "--disable-smart-routing"]
    if reason is not None:
        args += ["--smart-router-feedback", reason]

    result = runner.invoke(cli.app, args, env=env)

    assert result.exit_code == (0 if turned_off else 2), result.output
    saved = json.loads(session_file.read_text())
    assert saved == (dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0") if turned_off else {})


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_session_controls_reply_with_the_session_state(tmp_path, agent):
    session_file = tmp_path / "env.json"
    session_file.write_text("{}")
    env = {session_env.SESSION_ENV_VAR: str(session_file)}
    replies = []

    for flag in (
        "--disable-smart-routing",
        "--disable-smart-routing",
        "--enable-smart-routing",
        "--enable-smart-routing",
    ):
        result = runner.invoke(cli.app, [agent, flag], env=env)
        assert result.exit_code == 0, result.output
        replies.append(" ".join(result.output.split()))

    assert replies == [
        "✔ Smart Router is off for this session",
        "✔ Smart Router is already off for this session",
        "✔ Smart Router is on for this session",
        "✔ Smart Router is already on for this session",
    ]
