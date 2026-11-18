"""Codex's inline off prompt uses Isaac's existing evidence capture and uploader."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ucode.config_io import atomic_write_json
from ucode.os_compatibility import subprocess_cross_os
from ucode.smart_routing.session_env import effective_environment, session_env_path


def _isaac() -> str:
    executable = shutil.which("isaac")
    if not executable:
        raise RuntimeError("Isaac feedback is unavailable. Smart Router is still on.")
    return executable


def _target_path() -> Path:
    return session_env_path().with_name("feedback-target.json")


def _native_session_id() -> str:
    return str(UUID(os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID") or ""))


def _check_isaac_result(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    detail = f"{result.stderr or ''}\n{result.stdout or ''}".casefold()
    if any(
        error in detail
        for error in (
            "permission denied",
            "operation not permitted",
            "read-only file system",
            "access is denied",
        )
    ):
        raise RuntimeError(
            "Permission denied accessing Isaac feedback. Smart Router is still on. "
            "Request native command approval to retry the same off command."
        )
    raise ValueError("Isaac feedback command failed.")


def prepare_codex_feedback() -> None:
    """Anchor the preceding task before the user's reason becomes a new turn."""
    try:
        session_id = _native_session_id()
        result = subprocess_cross_os.run(
            [
                _isaac(),
                "feedback",
                "--inspect-target",
                "--harness",
                "codex",
                "--session-id",
                session_id,
            ],
            env=effective_environment(agent="codex", strict=True),
            capture_output=True,
            text=True,
            timeout=15,
        )
        _check_isaac_result(result)
        target = json.loads(result.stdout)
        if not isinstance(target, dict) or not isinstance(target.get("session_id"), str):
            raise ValueError("Missing native session identity.")
        if target["session_id"] != session_id:
            raise ValueError("Isaac resolved a different native session.")
        turn_id = target.get("turn_id") or target.get("message_id") or "no-prior-task"
        if not isinstance(turn_id, str) or len(turn_id) > 128:
            raise ValueError("Invalid native task identity.")
        atomic_write_json(_target_path(), {"session_id": session_id, "turn_id": turn_id})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Could not prepare Isaac feedback. Smart Router is still on; retry the off command."
        ) from exc


@dataclass(frozen=True)
class CapturedFeedback:
    executable: str
    feedback_id: str
    directory: Path
    notices: tuple[str, ...] = ()


def capture_codex_feedback(reason: str) -> CapturedFeedback:
    """Freeze evidence locally while routing is on; this never uploads it."""
    executable = _isaac()
    try:
        target = json.loads(_target_path().read_text(encoding="utf-8"))
        session_id = str(UUID(target["session_id"]))
        if session_id != _native_session_id():
            raise ValueError("Feedback belongs to a different native session.")
        turn_id = target["turn_id"]
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
            raise ValueError("Invalid feedback target.")
        feedback_id = str(uuid4())
        root = Path.home() / ".isaac" / "feedback"
        result = subprocess_cross_os.run(
            [
                executable,
                "feedback",
                "--capture-local",
                str(root),
                "--category",
                "smart-routing",
                "--harness",
                "codex",
                "--session-id",
                session_id,
                "--turn-id",
                turn_id,
                "--feedback-id",
                feedback_id,
                "--full-capture",
            ],
            input=reason,
            env=effective_environment(agent="codex", strict=True),
            capture_output=True,
            text=True,
            timeout=120,
        )
        _check_isaac_result(result)
        directory = root / feedback_id
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        if (
            receipt["feedback_id"] != feedback_id
            or receipt["request"]["message"] != reason
            or receipt["request"]["harness"] != "codex"
            or receipt["request"]["category"] != "smart-routing"
            or receipt["target"]["session_id"] != session_id
            or (
                turn_id != "no-prior-task"
                and turn_id
                not in (receipt["target"].get("turn_id"), receipt["target"].get("message_id"))
            )
        ):
            raise ValueError("Isaac captured a different feedback target.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "Could not save Isaac feedback. Smart Router is still on. "
            "Run the off command without a reason first, then provide the user's reason."
        ) from exc
    notices = tuple(
        notice
        for notice in (
            receipt["target"].get("notice"),
            receipt.get("smart_routing", {}).get("notice"),
        )
        if isinstance(notice, str) and notice
    )
    return CapturedFeedback(executable, feedback_id, directory, notices)


def submit_codex_feedback(feedback: CapturedFeedback) -> None:
    options: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with (feedback.directory / "upload.log").open("ab") as log:
        subprocess_cross_os.popen(
            [feedback.executable, "feedback", "--retry", feedback.feedback_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
            **options,
        )


def codex_feedback_status(feedback_id: str) -> str:
    report_id = str(UUID(feedback_id))
    directory = Path.home() / ".isaac" / "feedback" / report_id
    if not (directory / "receipt.json").is_file():
        raise ValueError("Feedback report is unavailable on this machine.")
    state_path = directory / "submission.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("Feedback delivery status is unavailable.")
    if state.get("evidence_status") in ("attached", "queued_for_verification"):
        return f"Feedback submitted; backend processing may be pending. Feedback ID: {report_id}"
    if state.get("last_error"):
        return (
            f"Feedback delivery is unconfirmed. Feedback ID: {report_id}. "
            f"Retry with /isaac-feedback retry {report_id}."
        )
    return (
        "Feedback saved; delivery is pending confirmation. You can keep working. "
        f"Feedback ID: {report_id}. Retry with /isaac-feedback retry {report_id} if needed."
    )
