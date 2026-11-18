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

runner = CliRunner()
SESSION_ID = "1cbd36a0-177f-4df1-a5c2-5495a86a04a7"
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
                    "target": {"session_id": SESSION_ID, "turn_id": TASK_ID},
                }
            )
        )
        return subprocess.CompletedProcess(args, 0, "Local feedback receipt saved", "")

    def upload(args, **kwargs):
        assert json.loads(controls.read_text()) == dict.fromkeys(v2.SMART_ROUTING_ENV_KEYS, "0")
        return Mock()

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
def test_isaac_permission_failure_requests_native_approval(inline_feedback, stage, error):
    controls, run, popen, _ = inline_feedback
    if stage == "capture":
        assert off().exit_code == 2
    run.side_effect = lambda *a, **k: subprocess.CompletedProcess([], 1, "", error)

    result = off(REASON if stage == "capture" else None)

    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "Permission denied accessing Isaac feedback" in text
    assert "Request native command approval" in text
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
