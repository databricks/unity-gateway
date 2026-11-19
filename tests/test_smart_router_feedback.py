"""Codex off collection with a controlled Isaac subprocess boundary."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ucode import cli
from ucode.smart_routing import feedback, session_env, v2
from ucode.smart_routing.codex_feedback import TOOL_NAME, TOOL_SPEC, CodexSmartRouter

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
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", "omni")
    monkeypatch.delenv("SMART_ROUTER_CONFIG_VERSION", raising=False)
    monkeypatch.setenv("ENABLE_SMART_ROUTING_V2", "0")
    monkeypatch.setenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", "1")
    controls = tmp_path / "session" / "env.json"
    controls.parent.mkdir()
    controls.write_text("{}")
    monkeypatch.setenv(session_env.SESSION_ENV_VAR, str(controls))
    monkeypatch.setattr(feedback.shutil, "which", lambda name: "/installed isaac")
    calls = []

    def isaac(args, **kwargs):
        calls.append((args, kwargs))
        assert json.loads(controls.read_text()) == {}, "capture must precede the toggle"
        if "--inspect-target" in args:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"session_id": SESSION_ID, "turn_id": TASK_ID}), ""
            )
        report_id = args[args.index("--feedback-id") + 1]
        directory = tmp_path / ".isaac" / "feedback" / report_id
        directory.mkdir(parents=True)
        (directory / "receipt.json").write_text(
            json.dumps(
                {
                    "feedback_id": report_id,
                    "request": {
                        "message": kwargs["input"],
                        "harness": "codex",
                        "category": "smart-routing",
                    },
                    "target": {
                        "session_id": SESSION_ID,
                        "turn_id": args[args.index("--turn-id") + 1],
                    },
                }
            )
        )
        return subprocess.CompletedProcess(args, 0, "Local feedback receipt saved", "")

    def upload(args, **kwargs):
        assert json.loads(controls.read_text()) == dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
        return Mock(poll=Mock(return_value=None))

    run = Mock(side_effect=isaac)
    popen = Mock(side_effect=upload)
    monkeypatch.setattr(feedback.subprocess_cross_os, "run", run)
    monkeypatch.setattr(feedback.subprocess_cross_os, "popen", popen)
    return controls, run, popen, calls


def off(reason=None):
    args = ["codex", "--disable-smart-routing"]
    if reason is not None:
        args.extend(["--smart-router-feedback", reason])
    return runner.invoke(cli.app, args)


@pytest.mark.parametrize("mode", ["omni", "direct", ""])
def test_codex_asks_in_every_launch_mode(inline_feedback, monkeypatch, mode):
    controls, run, popen, calls = inline_feedback
    monkeypatch.setenv("ISAAC_LAUNCH_MODE", mode)

    result = off()

    assert result.exit_code == 2, result.output
    text = " ".join(result.output.split())
    assert "16 characters" in text
    assert "session details will be sent to the Isaac team" in text
    assert "cancel to keep routing on" in text
    assert json.loads(controls.read_text()) == {}
    assert run.call_count == 1
    assert calls[0][0][-2:] == ["--session-id", SESSION_ID]
    assert json.loads(controls.with_name("feedback-target.json").read_text())["turn_id"] == TASK_ID
    popen.assert_not_called()


@pytest.mark.parametrize("reason", ["", "  too short  ", "界" * 15])
def test_short_reason_does_not_capture_or_disable(inline_feedback, reason):
    controls, run, popen, _ = inline_feedback
    assert off().exit_code == 2

    result = off(reason)

    assert result.exit_code == 2
    assert json.loads(controls.read_text()) == {}
    assert run.call_count == 1
    popen.assert_not_called()


def test_valid_reason_freezes_original_task_then_disables_then_submits(inline_feedback):
    controls, run, popen, calls = inline_feedback
    assert off().exit_code == 2

    result = off(REASON)

    assert result.exit_code == 0, result.output
    assert "Smart Router is off" in result.output
    assert "sending in the background" in result.output
    assert run.call_count == 2
    args, kwargs = calls[1]
    assert args[args.index("--turn-id") + 1] == TASK_ID
    assert args[args.index("--session-id") + 1] == SESSION_ID
    assert "--full-capture" in args
    assert kwargs["input"] == REASON
    assert kwargs["env"]["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
    report_id = args[args.index("--feedback-id") + 1]
    assert popen.call_args.args[0] == ["/installed isaac", "feedback", "--retry", report_id]
    assert popen.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert "Feedback ID: " + report_id in " ".join(result.output.split())

    repeated = off()
    assert repeated.exit_code == 0, repeated.output
    assert "already off" in repeated.output
    assert run.call_count == 2
    popen.assert_called_once()
    assert not session_env.session_routing_enabled(controls)


@pytest.mark.parametrize("reenable", ["cli", "native"])
def test_reenabled_session_cannot_upload_previous_tasks_evidence(
    native_router, inline_feedback, reenable
):
    controls, run, popen, calls = inline_feedback
    assert off().exit_code == 2
    assert off(REASON).exit_code == 0
    target = controls.with_name("feedback-target.json")
    if reenable == "cli":
        result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])
        assert result.exit_code == 0, result.output
    else:
        assert "is on" in native_call(native_router, "on")["message"]
    assert not target.exists()
    assert session_env.session_routing_enabled(controls)

    stale_attempt = off(REASON)
    assert stale_attempt.exit_code == 1, stale_attempt.output
    assert "still on" in stale_attempt.output
    assert run.call_count == 2
    assert popen.call_count == 1
    assert session_env.session_routing_enabled(controls)

    next_task = str(uuid4())
    isaac = run.side_effect

    def current_task(args, **kwargs):
        result = isaac(args, **kwargs)
        if "--inspect-target" in args:
            result.stdout = json.dumps({"session_id": SESSION_ID, "turn_id": next_task})
        return result

    run.side_effect = current_task
    assert off().exit_code == 2
    assert off(REASON).exit_code == 0
    captures = [args for args, _ in calls if "--capture-local" in args]
    assert [args[args.index("--turn-id") + 1] for args in captures] == [TASK_ID, next_task]
    assert popen.call_count == 2
    assert not session_env.session_routing_enabled(controls)


@pytest.mark.parametrize("reenable", ["cli", "native"])
def test_failed_task_reference_cleanup_keeps_routing_off(
    native_router, inline_feedback, monkeypatch, reenable
):
    import asyncio

    controls, run, popen, _ = inline_feedback
    assert off().exit_code == 2
    assert off(REASON).exit_code == 0
    target = controls.with_name("feedback-target.json")
    unlink = Path.unlink

    def deny_target_removal(path, *args, **kwargs):
        if path == target:
            raise PermissionError("target is locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_target_removal)
    if reenable == "cli":
        result = runner.invoke(cli.app, ["codex", "--enable-smart-routing"])
        assert result.exit_code == 1, result.output
        message = " ".join(result.output.split())
    else:
        result = asyncio.run(
            native_router.call({"threadId": SESSION_ID, "arguments": {"action": "on"}})
        )
        assert not result["success"]
        message = result["contentItems"][0]["text"]
    assert "Could not clear the saved Smart Router feedback task" in message
    assert "Routing is unchanged" in message
    assert not session_env.session_routing_enabled(controls)
    assert target.exists()
    assert run.call_count == 2
    assert popen.call_count == 1


@pytest.mark.parametrize("flag", ["0", "false", "off", ""])
def test_feedback_disabled_keeps_ordinary_toggle(inline_feedback, monkeypatch, flag):
    controls, run, popen, _ = inline_feedback
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", flag)

    assert off().exit_code == 0

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


@pytest.mark.parametrize(
    "failure", ["missing_isaac", "missing_session", "bad_inspection", "timeout"]
)
def test_unavailable_collection_keeps_routing_on(inline_feedback, monkeypatch, failure):
    controls, run, popen, _ = inline_feedback
    if failure == "missing_isaac":
        monkeypatch.setattr(feedback.shutil, "which", lambda name: None)
    elif failure == "missing_session":
        monkeypatch.delenv("CODEX_THREAD_ID")
        monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    elif failure == "bad_inspection":
        run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 0, "not json", "")
    else:
        run.side_effect = subprocess.TimeoutExpired("isaac", 15)

    result = off()

    assert result.exit_code == 1, result.output
    assert "still on" in result.output
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


def test_failed_capture_keeps_routing_on(inline_feedback):
    controls, run, popen, _ = inline_feedback
    assert off().exit_code == 2
    run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 1, "", "failure")

    result = off(REASON)

    assert result.exit_code == 1
    assert "still on" in result.output
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


@pytest.mark.parametrize("stage", ["prepare", "capture"])
@pytest.mark.parametrize(
    "error", ["permission denied", "read-only file system", "Access is denied"]
)
def test_isaac_permission_failure_keeps_routing_on(inline_feedback, stage, error):
    controls, run, popen, _ = inline_feedback
    if stage == "capture":
        assert off().exit_code == 2
    run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 1, "", error)

    result = off(REASON if stage == "capture" else None)

    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "Permission denied accessing Isaac feedback" in text
    assert "Check that Isaac can access" in text
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


def test_feedback_target_cannot_be_reused_in_another_session(inline_feedback, monkeypatch):
    controls, run, popen, _ = inline_feedback
    assert off().exit_code == 2
    monkeypatch.setenv("CODEX_THREAD_ID", str(uuid4()))

    result = off(REASON)

    assert result.exit_code == 1
    assert "still on" in result.output
    assert json.loads(controls.read_text()) == {}
    assert run.call_count == 1
    popen.assert_not_called()


def test_failed_toggle_does_not_upload_saved_preview(inline_feedback, monkeypatch):
    controls, _, popen, _ = inline_feedback
    assert off().exit_code == 2
    monkeypatch.setattr(
        cli, "set_session_environment", Mock(side_effect=RuntimeError("write denied"))
    )

    result = off(REASON)

    assert result.exit_code == 1
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


def test_failed_upload_start_keeps_routing_off_and_reports_retry(inline_feedback):
    controls, _, popen, _ = inline_feedback
    assert off().exit_code == 2
    popen.side_effect = OSError("cannot start uploader")

    result = off(REASON)

    assert result.exit_code == 0, result.output
    assert not session_env.session_routing_enabled(controls)
    assert "still off" in result.output
    assert "/isaac-feedback retry" in result.output


@pytest.mark.parametrize(
    "state,expected",
    [
        ({}, "delivery is pending confirmation"),
        ({"evidence_status": "queued_for_verification"}, "submitted; backend processing"),
        ({"evidence_status": "pending", "last_error": "network"}, "delivery is unconfirmed"),
    ],
)
def test_status_reports_delivery_without_resubmitting(inline_feedback, tmp_path, state, expected):
    _, run, popen, _ = inline_feedback
    report_id = str(uuid4())
    directory = tmp_path / ".isaac" / "feedback" / report_id
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text("{}")
    (directory / "submission.json").write_text(json.dumps(state))

    result = runner.invoke(cli.app, ["smart-router-feedback-status", "--feedback-id", report_id])

    assert result.exit_code == 0, result.output
    assert expected in " ".join(result.output.split())
    run.assert_not_called()
    popen.assert_not_called()


def test_malformed_delivery_status_reports_error(inline_feedback, tmp_path):
    report_id = str(uuid4())
    directory = tmp_path / ".isaac" / "feedback" / report_id
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text("{}")
    (directory / "submission.json").write_text("[]")

    result = runner.invoke(cli.app, ["smart-router-feedback-status", "--feedback-id", report_id])

    assert result.exit_code == 1
    assert "delivery status is unavailable" in result.output


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


def test_native_host_captures_then_disables_without_a_shell(native_router, inline_feedback):
    controls, run, popen, calls = inline_feedback
    prompt = native_call(native_router, "off")
    assert prompt["needs_reason"]
    assert "session details will be sent" in prompt["message"]
    assert native_call(native_router, "off")["needs_reason"]
    rejected = native_call(native_router, "off", reason="slow")
    assert rejected["validation_error"] == "reason_too_short"
    assert "4/16 characters" in rejected["message"]
    assert run.call_count == 1

    result = native_call(native_router, "off", reason=REASON)

    assert not session_env.session_routing_enabled(controls)
    assert "Smart Router is off" in result["message"]
    assert result["delivery"] == "pending"
    assert calls[1][1]["input"] == REASON
    assert calls[1][1]["env"]["CODEX_THREAD_ID"] == SESSION_ID
    assert calls[1][0][calls[1][0].index("--turn-id") + 1] == TASK_ID
    assert all(args[0] == "/installed isaac" for args, _ in calls)
    assert popen.call_args.kwargs["env"]["CODEX_THREAD_ID"] == SESSION_ID
    assert "already off" in native_call(native_router, "off")["message"]
    assert run.call_count == 2
    popen.assert_called_once()


def test_native_cancel_clears_pending_without_upload(native_router, inline_feedback):
    controls, run, popen, _ = inline_feedback
    assert native_call(native_router, "off")["needs_reason"]
    assert "remains on" in native_call(native_router, "cancel")["message"]
    assert SESSION_ID not in native_router.pending_threads
    with pytest.raises(ValueError, match="anchor the preceding task"):
        native_call(native_router, "off", reason=REASON)
    assert json.loads(controls.read_text()) == {}
    assert run.call_count == 1
    popen.assert_not_called()


def test_native_disabled_feedback_and_on_do_not_call_isaac(inline_feedback, monkeypatch):
    controls, run, popen, _ = inline_feedback
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "0")
    router = CodexSmartRouter(controls)
    router.root_threads.add(SESSION_ID)

    assert "is off" in native_call(router, "off")["message"]
    assert "is on" in native_call(router, "on")["message"]
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("thread_id", [str(uuid4()), None, {}])
def test_native_rejects_unbound_sessions(native_router, inline_feedback, thread_id):
    controls, run, popen, _ = inline_feedback
    with pytest.raises(ValueError, match="only to this session's root"):
        native_router.execute(
            {"threadId": thread_id, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}
        )
    assert json.loads(controls.read_text()) == {}
    run.assert_not_called()
    popen.assert_not_called()


def test_native_capture_failure_keeps_on(native_router, inline_feedback):
    controls, run, popen, _ = inline_feedback
    native_call(native_router, "off")
    run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 1, "", "capture failed")
    with pytest.raises(RuntimeError, match="still on"):
        native_call(native_router, "off", reason=REASON)
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


def test_native_upload_start_failure_keeps_off(native_router, inline_feedback):
    controls, _, popen, _ = inline_feedback
    native_call(native_router, "off")
    popen.side_effect = OSError("cannot start")

    result = native_call(native_router, "off", reason=REASON)

    assert not session_env.session_routing_enabled(controls)
    assert result["delivery"] == "unconfirmed"
    assert "retry this saved report" in result["message"]


def test_native_retry_reuses_same_report_without_capture(native_router, inline_feedback):
    _, run, popen, _ = inline_feedback
    native_call(native_router, "off")
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    native_call(native_router, "status", feedback_id=report_id)
    assert popen.call_count == 1

    native_call(native_router, "retry", feedback_id=report_id)

    assert run.call_count == 2
    assert popen.call_count == 2
    assert popen.call_args.args[0] == ["/installed isaac", "feedback", "--retry", report_id]


def test_native_retry_rejects_report_from_another_session(native_router, inline_feedback, tmp_path):
    _, run, popen, _ = inline_feedback
    native_call(native_router, "off")
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    receipt_path = tmp_path / ".isaac/feedback" / report_id / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["target"]["session_id"] = str(uuid4())
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="does not belong"):
        native_call(native_router, "retry", feedback_id=report_id)
    assert run.call_count == 2
    popen.assert_called_once()


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
    assert not router.root_threads
    router.observe({"id": 42, "result": {"thread": {"id": SESSION_ID}}})
    assert router.root_threads == {SESSION_ID}
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
    assert not router.root_threads


@pytest.mark.parametrize("tools", ["invalid", [{"name": TOOL_NAME}]])
def test_native_preserves_conflicting_user_tools(inline_feedback, tools):
    controls, *_ = inline_feedback
    router = CodexSmartRouter(controls)
    request = {"id": 1, "method": "thread/start", "params": {"dynamicTools": tools}}
    router.before_tui(request)
    assert request["params"]["dynamicTools"] == tools
    assert not router.thread_requests


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "off", "command": "arbitrary"},
        {"action": "off", "session_id": SESSION_ID},
        {"action": "off", "reason": []},
        {"action": "retry", "feedback_id": "../../report"},
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


def test_native_status_needs_no_isaac_and_malformed_receipt_returns_error(
    native_router, inline_feedback, tmp_path, monkeypatch
):
    import asyncio

    native_call(native_router, "off")
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    monkeypatch.setattr(feedback.shutil, "which", lambda _: None)
    assert native_call(native_router, "status", feedback_id=report_id)["delivery"] == "pending"
    receipt_path = tmp_path / ".isaac/feedback" / report_id / "receipt.json"
    receipt_path.write_text("[]")
    response = asyncio.run(
        native_router.call(
            {
                "threadId": SESSION_ID,
                "turnId": OFF_TURN_ID,
                "arguments": {"action": "retry", "feedback_id": report_id},
            }
        )
    )
    assert response["success"] is False
    assert "invalid" in response["contentItems"][0]["text"]


def test_codex_skill_requires_native_host_tool():
    text = (Path(__file__).parents[1] / "skills/smart-router/SKILL.md").read_text()
    assert "ug_smart_router" in text
    assert "do not fall back to Bash" in text
    assert "-m ucode.cli codex" not in text
    assert "sandbox_permissions" not in text


@pytest.mark.parametrize("exit_code", [0, 1])
def test_native_finished_uploader_without_confirmation_reports_failure(
    native_router, inline_feedback, exit_code
):
    native_call(native_router, "off")
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    native_router.uploads[report_id].poll.return_value = exit_code
    assert native_call(native_router, "status", feedback_id=report_id)["delivery"] == "unconfirmed"


def test_native_retry_ignores_stale_error_while_uploader_runs(
    native_router, inline_feedback, tmp_path
):
    native_call(native_router, "off")
    report_id = native_call(native_router, "off", reason=REASON)["feedback_id"]
    state = tmp_path / ".isaac/feedback" / report_id / "submission.json"
    state.write_text(json.dumps({"last_error": "old failure"}))
    result = native_call(native_router, "retry", feedback_id=report_id)
    assert result["delivery"] == "pending"
    assert "keep working" in result["message"]
    state.write_text(json.dumps({"evidence_status": "queued_for_verification"}))
    assert native_call(native_router, "status", feedback_id=report_id)["delivery"] == "pending"
    native_router.uploads[report_id].poll.return_value = 0
    assert native_call(native_router, "status", feedback_id=report_id)["delivery"] == "submitted"


def test_native_controls_write_failure_never_requests_shell_approval(
    native_router, inline_feedback, monkeypatch
):
    import asyncio

    controls, _, popen, _ = inline_feedback
    native_call(native_router, "off")
    monkeypatch.setattr(
        session_env, "atomic_write_json", Mock(side_effect=PermissionError("denied"))
    )

    async def ask(_):
        return ["Provide a reason", "user_note: " + REASON]

    response = asyncio.run(
        native_router.call(
            {"threadId": SESSION_ID, "turnId": OFF_TURN_ID, "arguments": {"action": "off"}}, ask
        )
    )
    assert response["success"] is False
    text = response["contentItems"][0]["text"]
    assert "launcher can access" in text
    assert "approval" not in text
    assert json.loads(controls.read_text()) == {}
    popen.assert_not_called()


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
    assert not native_router.pending_threads
    if reason is None:
        assert run.call_count == 1
        popen.assert_not_called()
    else:
        assert run.call_count == 2
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
    assert not native_router.pending_threads
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
    assert not native_router.pending_threads
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

    assert native_call(native_router, "off")["needs_reason"]
    assert run.call_count == 1, "Retry must preserve original task anchor"
    assert native_call(native_router, "off", reason=REASON)["feedback_id"]
    assert calls[1][0][calls[1][0].index("--turn-id") + 1] == TASK_ID
    assert calls[1][1]["input"] == REASON
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
