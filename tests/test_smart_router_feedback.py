"""Codex off collection with a controlled Isaac subprocess boundary."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import feedback, route_validation, session_env, session_log, v2
from ucode.smart_routing.codex_feedback import (
    TOOL_NAME,
    TOOL_SPEC,
    CodexSmartRouter,
    ThreadOwnership,
)

runner = CliRunner()
SESSION_ID = "1cbd36a0-177f-4df1-a5c2-5495a86a04a7"
OFF_TURN_ID = "off-command-turn"
TASK_ID = "44160a3a-e643-4259-bc9d-3a40b1803704"
REASON = "  The routed model didn't finish my task.\nPlease check its tool use.  "


@pytest.fixture
def inline_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CODEX_THREAD_ID", SESSION_ID)
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "1")
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", "direct")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("ENABLE_SMART_ROUTING_V2", "0")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    controls = tmp_path / "session" / "env.json"
    controls.parent.mkdir()
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.setattr(feedback.shutil, "which", lambda _: "/installed isaac")
    calls = []

    def isaac(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"session_id": SESSION_ID, "turn_id": TASK_ID}), ""
        )

    def upload(args, **kwargs):
        calls.append((args, {**kwargs, "input": kwargs["stdin"].read()}))
        return Mock(poll=Mock(return_value=0), wait=Mock(return_value=0))

    run = Mock(side_effect=isaac)
    popen = Mock(side_effect=upload)
    monkeypatch.setattr(feedback.subprocess_cross_os, "run", run)
    monkeypatch.setattr(feedback.subprocess_cross_os, "popen", popen)
    return controls, run, popen, calls


def off(reason=None, *, agent="codex", feedback_collected=False):
    args = [agent, "--disable-smart-routing"]
    if reason is not None:
        args.extend(["--smart-router-feedback", reason])
    if feedback_collected:
        args.append("--feedback-collected")
    return runner.invoke(cli.app, args)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["omni", "direct", "", None])
def test_cli_feedback_rule_by_agent_and_launch_mode(inline_feedback, monkeypatch, agent, mode):
    controls, run, popen, _ = inline_feedback
    if mode is None:
        monkeypatch.delenv("ISAAC_LAUNCH_MODE")
    else:
        monkeypatch.setenv("ISAAC_LAUNCH_MODE", mode)

    result = off(agent=agent)

    run.assert_not_called()
    popen.assert_not_called()
    assert not controls.with_name("feedback-target.json").exists()
    if mode == "omni" and agent == "codex":
        assert result.exit_code == 2, result.output
        text = " ".join(result.output.split())
        assert "switch off Smart Routing" in text
        assert "reason" not in text
        assert json.loads(controls.read_text()) == {}
        return
    if mode == "omni":
        assert result.exit_code == 0, result.output
        assert "Smart Router is off" in result.output
        assert not session_env.session_routing_enabled(controls)
        return
    assert result.exit_code == 2, result.output
    text = " ".join(result.output.split())
    assert "16 characters" in text
    assert "Smart Router skill to collect and submit feedback" in text
    assert "direct CLI command does not submit feedback" in text
    assert json.loads(controls.read_text()) == {}


@pytest.mark.parametrize(
    "mode,feedback_rule,reason,feedback_collected,expected_text",
    [
        pytest.param("omni", "1", None, True, None, id="omni-marked"),
        pytest.param(
            "omni", "1", REASON, False, "switch off Smart Routing", id="omni-reason-unmarked"
        ),
        pytest.param(
            "omni", "0", None, False, "switch off Smart Routing", id="omni-feedback-rule-off"
        ),
        pytest.param("direct", "1", None, True, "16 characters", id="direct-marker-ignored"),
        pytest.param("direct", "1", REASON, True, None, id="direct-reason"),
        pytest.param("direct", "0", None, False, None, id="direct-feedback-rule-off"),
    ],
)
def test_codex_cli_off_needs_the_marker_in_omnigent(
    inline_feedback, monkeypatch, mode, feedback_rule, reason, feedback_collected, expected_text
):
    controls, run, popen, _ = inline_feedback
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", mode)
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", feedback_rule)

    result = off(reason, feedback_collected=feedback_collected)

    turned_off = expected_text is None
    assert result.exit_code == (0 if turned_off else 2), result.output
    assert session_env.session_routing_enabled(controls) is not turned_off
    text = " ".join(result.output.split())
    if expected_text is not None:
        assert expected_text in text
    if expected_text == "switch off Smart Routing":
        assert "reason" not in text
    run.assert_not_called()
    popen.assert_not_called()


def test_codex_cli_on_and_repeated_off_need_no_marker(inline_feedback, monkeypatch):
    controls, _, _, _ = inline_feedback
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", "omni")
    assert off(feedback_collected=True).exit_code == 0

    repeated = off()
    assert repeated.exit_code == 0, repeated.output
    assert "already off" in repeated.output

    result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])
    assert result.exit_code == 0, result.output
    assert "Smart Router is on for this session" in " ".join(result.output.split())
    assert session_env.session_routing_enabled(controls)


@pytest.mark.parametrize("reason", ["", "  too short  ", "界" * 15])
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_short_cli_reason_does_not_capture_or_disable(inline_feedback, reason, agent):
    controls, run, popen, _ = inline_feedback

    result = off(reason, agent=agent)

    assert result.exit_code == 2
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_cli_reason_toggles_without_capturing_or_uploading(inline_feedback, monkeypatch, agent):
    controls, run, popen, _ = inline_feedback
    monkeypatch.setattr(feedback.shutil, "which", lambda _: None)
    assert off(agent=agent).exit_code == 2

    result = off(REASON, agent=agent)

    assert result.exit_code == 0, result.output
    assert "Smart Router is off" in result.output
    assert not controls.with_name("feedback-target.json").exists()
    assert "Feedback ID:" not in result.output

    repeated = off(agent=agent)
    assert repeated.exit_code == 0, repeated.output
    assert "already off" in repeated.output
    run.assert_not_called()
    popen.assert_not_called()
    assert not session_env.session_routing_enabled(controls)


@pytest.mark.parametrize("reenable", ["claude", "codex", "native"])
def test_native_off_after_reenable_identifies_current_task(
    native_router, inline_feedback, reenable
):
    controls, run, popen, calls = inline_feedback
    assert native_call(native_router, "off")["needs_reason"]
    assert native_call(native_router, "off", reason=REASON)["feedback_id"]
    if reenable == "native":
        assert "is on" in native_call(native_router, "on")["message"]
    else:
        assert runner.invoke(cli.app, [reenable, "--enable-smart-routing"]).exit_code == 0
    assert session_env.session_routing_enabled(controls)
    assert not native_router.pending_feedback
    next_task = str(uuid4())
    run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"session_id": SESSION_ID, "turn_id": next_task}), ""
    )
    run.side_effect = None
    assert native_call(native_router, "off")["needs_reason"]
    assert native_call(native_router, "off", reason=REASON)["feedback_id"]
    submissions = [args for args, _ in calls if "--with-evidence" in args]
    assert [args[args.index("--turn-id") + 1] for args in submissions] == [TASK_ID, next_task]
    assert popen.call_count == 2
    assert not controls.with_name("feedback-target.json").exists()


@pytest.mark.parametrize("flag", ["0", "false", "off", ""])
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_feedback_disabled_keeps_ordinary_toggle(inline_feedback, monkeypatch, flag, agent):
    controls, run, popen, _ = inline_feedback
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", flag)

    assert off(agent=agent).exit_code == 0

    assert not session_env.session_routing_enabled(controls)
    run.assert_not_called()
    popen.assert_not_called()


def test_turning_on_needs_no_feedback(inline_feedback):
    controls, run, popen, _ = inline_feedback
    controls.write_text(json.dumps(dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")))

    result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])

    assert result.exit_code == 0
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("failure", ["missing_isaac", "bad_inspection", "timeout"])
def test_unavailable_native_collection_keeps_routing_on(
    native_router, inline_feedback, monkeypatch, failure
):
    import asyncio

    controls, run, popen, _ = inline_feedback
    if failure == "missing_isaac":
        monkeypatch.setattr(feedback.shutil, "which", lambda name: None)
    elif failure == "bad_inspection":
        run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 0, "not json", "")
    else:
        run.side_effect = subprocess.TimeoutExpired("isaac", 15)

    result = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}
        )
    )
    assert not result["success"], result
    assert ("unavailable" if failure == "missing_isaac" else "still on") in result["contentItems"][
        0
    ]["text"]
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


def test_native_rejects_inspection_of_another_session(native_router, inline_feedback):
    controls, run, popen, _ = inline_feedback
    run.side_effect = None
    run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"session_id": str(uuid4()), "turn_id": TASK_ID}), ""
    )
    with pytest.raises(RuntimeError, match="still on"):
        native_call(native_router, "off")
    assert session_env.session_routing_enabled(controls)
    popen.assert_not_called()


@pytest.fixture
def native_router(inline_feedback):
    controls, *_ = inline_feedback
    router = CodexSmartRouter(controls)
    request = {"id": "start", "method": "thread/start", "params": {}}
    router.before_tui(request)
    assert request["params"]["dynamicTools"][0]["name"] == TOOL_NAME
    router.observe({"id": "start", "result": {"thread": {"id": SESSION_ID}}})
    register_off_command(router)
    return router


def register_off_command(router, turn_id=OFF_TURN_ID):
    router.before_tui(
        {
            "id": "off-command",
            "method": "turn/start",
            "params": {
                "threadId": SESSION_ID,
                "input": [
                    {"type": "text", "text": "$smart-router off"},
                    {
                        "type": "skill",
                        "name": "smart-router",
                        "path": "/skills/smart-router/SKILL.md",
                    },
                ],
            },
        }
    )
    router.observe({"id": "off-command", "result": {"turn": {"id": turn_id}}})


def native_call(router, action, **kwargs):
    return router.execute(
        {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": action, **kwargs}}
    )


def test_native_starts_isaac_with_evidence_and_then_disables(native_router, inline_feedback):
    controls, run, popen, calls = inline_feedback
    assert native_call(native_router, "off")["needs_reason"]
    assert run.call_args.args[0] == [
        "/installed isaac",
        "feedback",
        "--inspect-target",
        "--harness",
        "codex",
        "--session-id",
        SESSION_ID,
    ]
    start = popen.side_effect

    def before_disable(args, **kwargs):
        assert session_env.session_routing_enabled(controls)
        return start(args, **kwargs)

    popen.side_effect = before_disable
    result = native_call(native_router, "off", reason=REASON)
    args, kwargs = calls[-1]
    assert args == [
        "/installed isaac",
        "feedback",
        "--category",
        "smart-routing",
        "--harness",
        "codex",
        "--session-id",
        SESSION_ID,
        "--feedback-id",
        result["feedback_id"],
        "--with-evidence",
        "--turn-id",
        TASK_ID,
    ]
    assert kwargs["input"] == REASON
    assert kwargs["env"]["CODEX_THREAD_ID"] == SESSION_ID
    assert kwargs["env"]["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
    assert kwargs["env"][session_env.SESSION_ENV_VAR] == ""
    assert "--capture-local" not in args
    assert not session_env.session_routing_enabled(controls)
    assert result["delivery"] == "pending"
    assert "already off" in native_call(native_router, "off")["message"]
    run.assert_called_once()
    popen.assert_called_once()
    assert not controls.with_name("feedback-target.json").exists()


def test_native_without_prior_task_uses_isaacs_empty_task_selector(native_router, inline_feedback):
    _, run, popen, _ = inline_feedback
    run.side_effect = None
    run.return_value = subprocess.CompletedProcess(
        [], 0, json.dumps({"session_id": SESSION_ID, "turn_id": None}), ""
    )
    assert native_call(native_router, "off")["needs_reason"]
    result = native_call(native_router, "off", reason=REASON)
    assert popen.call_args.args[0][-2:] == ["--turn-id", "no-prior-task"]
    assert "No earlier task" in result["notices"][0]


def test_native_cancel_clears_pending_without_upload(native_router, inline_feedback):
    controls, run, popen, _ = inline_feedback
    assert native_call(native_router, "off")["needs_reason"]
    assert "remains on" in native_call(native_router, "cancel")["message"]
    assert SESSION_ID not in native_router.pending_feedback
    with pytest.raises(ValueError, match="feedback prompt first"):
        native_call(native_router, "off", reason=REASON)
    assert json.loads(controls.read_text()) == {}
    assert run.call_count == 1
    popen.assert_not_called()


@pytest.mark.parametrize("flag,mode", [("0", "direct"), ("1", "omni"), ("0", "omni")])
def test_native_feedback_exemptions_skip_prompt_and_isaac(inline_feedback, monkeypatch, flag, mode):
    import asyncio
    from unittest.mock import AsyncMock

    controls, run, popen, _ = inline_feedback
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", flag)
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", mode)
    router = CodexSmartRouter(controls)
    router.threads[SESSION_ID] = ThreadOwnership.ROOT
    register_off_command(router)
    ask = AsyncMock()

    response = asyncio.run(
        router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}, ask
        )
    )
    assert response["success"], response
    assert "is off" in response["contentItems"][0]["text"]
    assert not session_env.session_routing_enabled(controls)
    assert not controls.with_name("feedback-target.json").exists()
    assert "is on" in native_call(router, "on")["message"]
    assert json.loads(controls.read_text()) == {}
    ask.assert_not_called()
    run.assert_not_called()
    popen.assert_not_called()


# A fixed id keeps collection identical across pytest-xdist workers.
@pytest.mark.parametrize("thread_id", ["5b0f6c1e-2d47-4b8e-9a3c-7e1d2f4a6b90", None, {}])
def test_native_rejects_unbound_sessions(native_router, inline_feedback, thread_id):
    controls, run, popen, _ = inline_feedback
    with pytest.raises(ValueError, match="only to this session's root"):
        native_router.execute(
            {"threadId": thread_id, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}
        )
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_native_delivery_result_matches_isaac_exit(native_router, inline_feedback, exit_code):
    import asyncio

    controls, _, _, _ = inline_feedback
    assert native_call(native_router, "off")["needs_reason"]
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    process = native_router.uploads.pop(report_id)
    process.poll.return_value = exit_code
    result = asyncio.run(native_router.delivery(report_id, process))
    assert result["delivery"] == ("submitted" if exit_code == 0 else "unconfirmed")
    assert not session_env.session_routing_enabled(controls)
    assert not native_router.uploads


def test_native_upload_start_failure_keeps_off(native_router, inline_feedback):
    controls, _, popen, _ = inline_feedback
    native_call(native_router, "off")
    popen.side_effect = OSError("cannot start")

    result = native_call(native_router, "off", reason=REASON)

    assert not session_env.session_routing_enabled(controls)
    assert result["delivery"] == "unconfirmed"
    assert "delivery could not start" in result["message"]


@pytest.mark.parametrize("method", ["thread/start", "thread/resume"])
def test_native_binds_only_correlated_root_responses(inline_feedback, method):
    controls, *_ = inline_feedback
    router = CodexSmartRouter(controls)
    existing = {"name": "user_tool", "description": "preserve"}
    request = {"id": 42, "method": method, "params": {"dynamicTools": [existing]}}
    router.before_tui(request)
    if method == "thread/start":
        assert request["params"]["dynamicTools"] == [existing, TOOL_SPEC]
    else:
        assert request["params"]["dynamicTools"] == [existing]
    router.observe({"id": 41, "result": {"thread": {"id": SESSION_ID}}})
    router.observe({"id": 42, "method": "item/tool/call", "params": {"tool": "caller_tool"}})
    assert not router.threads
    assert 42 in router.thread_requests
    router.observe({"id": 42, "result": {"thread": {"id": SESSION_ID}}})
    assert router.threads == {SESSION_ID: ThreadOwnership.ROOT}
    assert not router.thread_requests


@pytest.mark.parametrize(
    "thread",
    [
        {"id": SESSION_ID, "parentThreadId": "parent"},
        {"id": SESSION_ID, "source": {"subAgent": {}}},
        {"id": "invalid"},
    ],
)
def test_native_does_not_bind_child_or_malformed_thread(inline_feedback, thread):
    controls, *_ = inline_feedback
    router = CodexSmartRouter(controls)
    router.before_tui({"id": 1, "method": "thread/resume", "params": {}})
    router.observe({"id": 1, "result": {"thread": thread}})
    assert not router.threads


@pytest.mark.parametrize("tools", ["invalid", [{"name": TOOL_NAME}]])
@pytest.mark.parametrize("method", ["thread/start", "thread/resume"])
def test_native_preserves_conflicting_user_tools(inline_feedback, tools, method):
    controls, *_ = inline_feedback
    router = CodexSmartRouter(controls)
    request = {"id": 1, "method": method, "params": {"dynamicTools": tools}}
    router.before_tui(request)
    assert request["params"]["dynamicTools"] == tools
    if tools == "invalid":
        assert not router.thread_requests
        return
    assert router.thread_requests == {1: False}
    router.observe({"id": 1, "result": {"thread": {"id": SESSION_ID}}})
    assert not router.thread_requests
    assert router.threads == {SESSION_ID: ThreadOwnership.CALLER}
    assert not router.handles(
        {
            "id": "caller-tool",
            "method": "item/tool/call",
            "params": {
                "threadId": SESSION_ID,
                "turnId": "caller-turn",
                "tool": TOOL_NAME,
                "arguments": {"action": "on"},
            },
        }
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "off", "command": "arbitrary"},
        {"action": "off", "session_id": SESSION_ID},
        {"action": "off", "reason": []},
        {"action": "status", "reason": REASON},
    ],
)
def test_native_invalid_calls_return_errors_without_mutation(
    native_router, inline_feedback, arguments
):
    import asyncio

    controls, run, popen, _ = inline_feedback
    response = asyncio.run(
        native_router.call({"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": arguments})
    )
    assert response["success"] is False
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


def test_codex_skill_falls_back_to_exact_cli_toggles_without_the_native_tool():
    text = (Path(__file__).parents[1] / "skills/smart-router/SKILL.md").read_text()
    commands = [
        '"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --enable-smart-routing',
        '"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing',
    ]
    assert "ug_smart_router" in text
    for command in commands:
        assert f"`{command}`" in text
        assert f"Bash({command})" in text
    assert "-m ucode.cli codex --disable-smart-routing --" not in text
    assert "sandbox_permissions" not in text


def test_feedback_worker_stops_and_reaps_on_timeout():
    import asyncio

    process = Mock(poll=Mock(return_value=None))
    assert asyncio.run(feedback.wait_for_feedback(process, timeout=0.01)) is False
    process.kill.assert_called_once()
    process.wait.assert_called_once()


def test_closing_codex_leaves_detached_feedback_running():
    import asyncio

    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read(); print('finished')"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    async def run():
        task = asyncio.create_task(feedback.wait_for_feedback(process))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)

    try:
        asyncio.run(run())
        assert process.poll() is None, "Closing Codex killed feedback already sending"
        stdout, stderr = process.communicate("", timeout=3)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "finished"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)


def test_native_toggle_failure_does_not_cancel_feedback_or_request_approval(
    native_router, inline_feedback, monkeypatch
):
    import asyncio
    from unittest.mock import AsyncMock

    controls, _, popen, _ = inline_feedback
    monkeypatch.setattr(
        session_env, "atomic_write_json", Mock(side_effect=PermissionError("denied"))
    )
    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}},
            AsyncMock(return_value=["Provide a reason", "user_note: " + REASON]),
        )
    )
    assert response["success"], response
    result = json.loads(response["contentItems"][0]["text"])
    assert "Could not turn Smart Router off" in result["message"]
    assert "Routing is unchanged" in result["message"]
    assert "approval" not in result["message"]
    assert result["delivery"] == "pending"
    assert session_env.session_routing_enabled(controls)
    popen.assert_called_once()


@pytest.mark.parametrize(
    "answers,reason",
    [
        ([["Provide a reason", "user_note: " + REASON]], REASON),
        ([["Provide a reason"], ["user_note: " + REASON]], REASON),
        ([["Provide a reason"], ["user_note: " + "界" * 16]], "界" * 16),
        ([["Cancel", "user_note: " + REASON]], None),
        ([None], None),
        ([["Provide a reason"], []], None),
    ],
)
def test_native_selector_collects_within_toggle_call(
    native_router, inline_feedback, answers, reason
):
    import asyncio

    controls, run, popen, calls = inline_feedback
    questions = []
    replies = iter(answers)

    async def ask(question):
        assert session_env.session_routing_enabled(controls)
        assert run.call_count == 1, "Only the original task is prepared before reason acceptance"
        questions.append(question)
        return next(replies)

    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}},
            ask,
        )
    )
    assert response["success"], response
    assert [option["label"] for option in questions[0]["options"]] == ["Provide a reason", "Cancel"]
    assert questions[0]["isOther"] is False
    assert all("Isaac team" in question["question"] for question in questions)
    assert all(question["options"] is None for question in questions[1:])
    assert session_env.session_routing_enabled(controls) == (reason is None)
    assert not native_router.pending_feedback
    if reason is None:
        assert run.call_count == 1
        popen.assert_not_called()
    else:
        assert run.call_count == 1
        assert calls[1][1]["input"] == reason
        assert calls[1][0][calls[1][0].index("--turn-id") + 1] == TASK_ID
        popen.assert_called_once()


@pytest.mark.parametrize(
    "answer",
    [
        ["unknown"],
        ["Provide a reason", "invented text"],
        ["Provide a reason", "user_note: first", "user_note: second"],
    ],
)
def test_native_invalid_selector_response_never_submits(native_router, inline_feedback, answer):
    import asyncio

    controls, run, popen, _ = inline_feedback

    async def ask(_):
        return answer

    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}, ask
        )
    )
    assert not response["success"]
    assert session_env.session_routing_enabled(controls)
    assert not native_router.pending_feedback
    popen.assert_not_called()
    assert run.call_count == 1


@pytest.mark.parametrize("reason", [REASON, "slow"])
def test_native_model_supplied_reason_cannot_bypass_prompt(native_router, inline_feedback, reason):
    import asyncio
    from unittest.mock import AsyncMock

    controls, run, popen, _ = inline_feedback
    ask = AsyncMock()
    response = asyncio.run(
        native_router.call(
            {
                "threadId": SESSION_ID,
                "turnId": OFF_TURN_ID,
                "arguments": {"action": "off", "reason": reason},
            },
            ask,
        )
    )
    assert not response["success"]
    assert "feedback prompt" in response["contentItems"][0]["text"]
    ask.assert_not_called()
    run.assert_not_called()
    popen.assert_not_called()
    assert session_env.session_routing_enabled(controls)


def test_native_canceled_collection_clears_pending_and_never_submits(
    native_router, inline_feedback
):
    import asyncio

    controls, run, popen, _ = inline_feedback

    async def run_test():
        opened = asyncio.Event()

        async def ask(_):
            opened.set()
            await asyncio.Future()

        task = asyncio.create_task(
            native_router.call(
                {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}, ask
            )
        )
        await asyncio.wait_for(opened.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_test())
    assert not native_router.pending_feedback
    assert not native_router.collecting_threads
    assert session_env.session_routing_enabled(controls)
    assert run.call_count == 1
    popen.assert_not_called()


def test_native_duplicate_off_cannot_bypass_open_selector(native_router, inline_feedback):
    import asyncio

    controls, run, popen, _ = inline_feedback

    async def run_test():
        opened = asyncio.Event()
        answer = asyncio.get_running_loop().create_future()

        async def ask(_):
            opened.set()
            return await answer

        task = asyncio.create_task(
            native_router.call(
                {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}, ask
            )
        )
        await asyncio.wait_for(opened.wait(), 2)
        duplicate = await native_router.call(
            {
                "threadId": SESSION_ID,
                "turnId": OFF_TURN_ID,
                "arguments": {"action": "off", "reason": REASON},
            },
            ask,
        )
        assert not duplicate["success"]
        assert "Use $smart-router off" in duplicate["contentItems"][0]["text"]
        answer.set_result(["Cancel"])
        assert (await asyncio.wait_for(task, 2))["success"]

    asyncio.run(run_test())
    assert session_env.session_routing_enabled(controls)
    assert run.call_count == 1
    popen.assert_not_called()


@pytest.mark.parametrize("reason", ["", "slow", "   slow   ", "界" * 15])
@pytest.mark.parametrize("entry", ["text", "option_note"])
def test_native_short_reason_ends_attempt_without_reopening(
    native_router, inline_feedback, reason, entry
):
    import asyncio

    controls, run, popen, calls = inline_feedback
    questions = []

    async def ask(question):
        questions.append(question)
        assert len(questions) <= (2 if entry == "text" else 1), (
            "Short answer must not reopen the prompt"
        )
        if entry == "text" and len(questions) == 1:
            return ["Provide a reason"]
        return (["Provide a reason"] if entry == "option_note" else []) + ["user_note: " + reason]

    arguments = {"action": "off"}
    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": arguments}, ask
        )
    )
    assert response["success"], response
    result = json.loads(response["contentItems"][0]["text"])
    assert result["validation_error"] == "reason_too_short"
    assert f"{len(reason.strip())}/16 characters" in result["message"]
    assert "Smart Router is still on; nothing was sent" in result["message"]
    assert "needs_reason" not in result
    assert len(questions) == {"text": 2, "option_note": 1}[entry]
    assert not native_router.collecting_threads
    assert session_env.session_routing_enabled(controls)
    assert run.call_count == 1
    popen.assert_not_called()

    from unittest.mock import AsyncMock

    register_off_command(native_router, turn_id="retry-turn")
    retry = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": "retry-turn", "arguments": {"action": "off"}},
            AsyncMock(return_value=["Provide a reason", "user_note: " + REASON]),
        )
    )
    assert retry["success"], retry
    assert run.call_count == 2
    assert calls[-1][0][calls[-1][0].index("--turn-id") + 1] == TASK_ID
    assert calls[-1][1]["input"] == REASON
    popen.assert_called_once()


@pytest.mark.parametrize(
    "text,selected_skill,allowed",
    [
        ("$smart-router off", True, True),
        (" /smart-router off\n", True, True),
        ("$smart-router off", False, False),
        ("Turn off Smart Router.", False, False),
        ("Turn off Smart Router. My reason: delegation is slow and models are worse.", True, False),
        ("$smart-router off because delegation is slow", True, False),
        ("`$smart-router off`", True, False),
        ("Explain $smart-router off", True, False),
        ("$smart-router on", True, False),
    ],
)
def test_native_off_requires_explicit_skill_command_in_current_turn(
    native_router, inline_feedback, text, selected_skill, allowed
):
    import asyncio
    from unittest.mock import AsyncMock

    controls, run, upload, _ = inline_feedback
    native_router.off_turns.clear()
    inputs = [{"type": "text", "text": text}]
    if selected_skill:
        inputs.append(
            {"type": "skill", "name": "smart-router", "path": "/skills/smart-router/SKILL.md"}
        )
    native_router.before_tui(
        {
            "id": "command-check",
            "method": "turn/start",
            "params": {"threadId": SESSION_ID, "input": inputs},
        }
    )
    native_router.observe({"id": "command-check", "result": {"turn": {"id": "current-turn"}}})
    ask = AsyncMock(return_value=["Cancel"])
    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": "current-turn", "arguments": {"action": "off"}}, ask
        )
    )
    assert response["success"] == allowed
    assert ask.call_count == int(allowed)
    assert run.call_count == int(allowed)
    assert session_env.session_routing_enabled(controls)
    upload.assert_not_called()
    if not allowed:
        assert "Use $smart-router off" in response["contentItems"][0]["text"]


@pytest.mark.parametrize("ending", ["used", "completed", "foreign_turn", "foreign_thread"])
def test_native_off_command_authorization_cannot_be_reused(native_router, inline_feedback, ending):
    import asyncio
    from unittest.mock import AsyncMock

    controls, run, upload, _ = inline_feedback
    params = {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}
    ask = AsyncMock(return_value=["Cancel"])
    if ending == "completed":
        native_router.observe(
            {
                "method": "turn/completed",
                "params": {"threadId": SESSION_ID, "turn": {"id": OFF_TURN_ID}},
            }
        )
    elif ending == "foreign_turn":
        params["turnId"] = "ordinary-chat-turn"
    elif ending == "foreign_thread":
        params["threadId"] = str(uuid4())
    else:
        assert asyncio.run(native_router.call(params, ask))["success"]
    before = run.call_count
    response = asyncio.run(native_router.call(params, ask))
    assert not response["success"]
    assert run.call_count == before
    assert session_env.session_routing_enabled(controls)
    upload.assert_not_called()


@pytest.mark.parametrize("reply_error", [False, True])
def test_native_off_waits_for_correlated_turn_start_reply(
    native_router, inline_feedback, reply_error
):
    import asyncio
    from unittest.mock import AsyncMock

    controls, run, upload, _ = inline_feedback

    async def scenario():
        native_router.before_tui(
            {
                "id": "delayed-command",
                "method": "turn/start",
                "params": {
                    "threadId": SESSION_ID,
                    "input": [
                        {"type": "text", "text": "$smart-router off"},
                        {
                            "type": "skill",
                            "name": "smart-router",
                            "path": "/skills/smart-router/SKILL.md",
                        },
                    ],
                },
            }
        )
        ask = AsyncMock(return_value=["Cancel"])
        task = asyncio.create_task(
            native_router.call(
                {"threadId": SESSION_ID, "turnId": "delayed-turn", "arguments": {"action": "off"}},
                ask,
            )
        )
        await asyncio.sleep(0)
        native_router.observe({"id": "unrelated", "result": {"turn": {"id": "delayed-turn"}}})
        native_router.observe(
            {"id": "delayed-command", "method": "item/tool/call", "params": {"tool": "caller_tool"}}
        )
        await asyncio.sleep(0)
        assert not task.done()
        reply = (
            {"error": {"message": "turn did not start"}}
            if reply_error
            else {"result": {"turn": {"id": "delayed-turn"}}}
        )
        native_router.observe({"id": "delayed-command", **reply})
        response = await asyncio.wait_for(task, 2)
        assert response["success"] == (not reply_error)
        assert ask.call_count == int(not reply_error)

    asyncio.run(scenario())
    assert run.call_count == int(not reply_error)
    assert session_env.session_routing_enabled(controls)
    upload.assert_not_called()


UNSUPPORTED_CHECK = {
    "routing_disabled": True,
    "router_name": "stable",
    "routing_unsupported_reason": "insufficient_eligible_options",
    "eligible_route_options": [],
    "ineligible_route_options": [],
    "available_route_options": [{"model": "gpt-5", "harness": "codex"}],
    "validation_ms": 120,
}
FAILED_CHECK = {
    "routing_disabled": True,
    "router_name": "stable",
    "validation_failed": True,
    "validation_failure_reason": "Smart Router didn't respond in time",
    "eligible_route_options": [],
    "ineligible_route_options": [],
    "available_route_options": [],
    "validation_ms": 4000,
}
SUPPORTED_CHECK = {"routing_disabled": False}
CHECK_RAISES = RuntimeError("token unavailable")
UNEXPECTED_FAILURE_CHECK = {
    **FAILED_CHECK,
    "validation_failure_reason": route_validation.CHECK_FAILED_REASON,
}


@pytest.fixture
def session_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(session_log, "LOG_DIR", tmp_path / "sessions")


def toggle_router(controls, validate):
    router = CodexSmartRouter(controls, validate)
    request = {"id": "start", "method": "thread/start", "params": {}}
    router.before_tui(request)
    router.observe({"id": "start", "result": {"thread": {"id": SESSION_ID}}})
    return router


@pytest.mark.parametrize(
    ("initially_on", "outcome", "enabled", "message", "published"),
    [
        pytest.param(
            False,
            SUPPORTED_CHECK,
            True,
            "Smart Router is on for this session.",
            None,
            id="turn-on-supported",
        ),
        pytest.param(
            True,
            SUPPORTED_CHECK,
            True,
            "Smart Router is already on for this session.",
            None,
            id="already-on-supported",
        ),
        pytest.param(
            False,
            UNSUPPORTED_CHECK,
            False,
            "Smart Router is off for this session.",
            UNSUPPORTED_CHECK,
            id="turn-on-unsupported",
        ),
        pytest.param(
            True,
            UNSUPPORTED_CHECK,
            False,
            "Smart Router is off for this session.",
            UNSUPPORTED_CHECK,
            id="already-on-unsupported",
        ),
        pytest.param(
            False,
            FAILED_CHECK,
            False,
            "Smart Router is off for this session.",
            FAILED_CHECK,
            id="turn-on-failed-check",
        ),
        pytest.param(
            False,
            CHECK_RAISES,
            False,
            "Smart Router is off for this session.",
            UNEXPECTED_FAILURE_CHECK,
            id="turn-on-check-raises",
        ),
    ],
)
def test_on_checks_the_model_list_before_turning_routing_on(
    inline_feedback, session_logs, initially_on, outcome, enabled, message, published
):
    controls, *_ = inline_feedback
    if not initially_on:
        controls.write_text(json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")))

    def validate():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    router = toggle_router(controls, validate)
    assert native_call(router, "on") == {"message": message}
    assert session_env.session_routing_enabled(controls) is enabled
    result_path = session_env.validation_result_path(controls)
    records = session_log.read_records("codex", SESSION_ID, session_log.ROUTE_VALIDATION_TYPE)
    if published is None:
        assert not result_path.exists()
        assert records == []
        return
    result = json.loads(result_path.read_text())
    [row] = records
    if isinstance(outcome, Exception):
        published = {**published, "validation_ms": result["validation_ms"]}
    assert result == {**published, "logged": True}
    # The row is already written, so the first-prompt recorder must not log it again.
    assert session_env.read_unsupported_validation(controls) is None
    assert {key: row[key] for key in published} == published
    assert "Reason:" in session_log.render_validation_detail(row)


@pytest.mark.parametrize(
    ("initial", "action"),
    [("{}", "on"), (json.dumps(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0")), "off")],
    ids=["on", "off-while-already-off"],
)
def test_toggling_clears_the_previous_warning(inline_feedback, session_logs, initial, action):
    controls, *_ = inline_feedback
    controls.write_text(initial)
    result_path = session_env.validation_result_path(controls)
    result_path.write_text(json.dumps(UNSUPPORTED_CHECK))
    session_env.validation_dismissed_path(controls).touch()
    router = toggle_router(controls, lambda: SUPPORTED_CHECK)
    native_call(router, action)
    assert not result_path.exists()
    assert not session_env.validation_dismissed_path(controls).exists()
