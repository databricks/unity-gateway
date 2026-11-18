"""Session-bound Smart Router tool executed by the UG launcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from websockets.exceptions import ConnectionClosed

from ucode.constants import ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR, SMART_ROUTING_ENV_KEYS
from ucode.env import env_bool
from ucode.smart_routing import feedback
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    session_routing_enabled,
    set_session_environment,
)

TOOL_NAME = "ug_smart_router"
PROVIDE_REASON = "Provide a reason"
CANCEL = "Cancel"
FEEDBACK_QUESTION_ID = "smart_router_feedback"
FEEDBACK_DISCLOSURE = "Your reason and session details will be sent to the Isaac team."
AskFeedback = Callable[[dict], Awaitable[list[str] | None]]
TOOL_SPEC = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Control Unity Gateway Smart Router for this root session at the user's request. "
        "Use this native tool instead of shell commands. Call off without a reason first; "
        "if feedback is required, the launcher opens a native choice menu and collects the reason. "
        "The user can provide a reason (minimum 16 trimmed characters) or cancel. "
        "Do not ask another question or switch to Plan mode. "
        "If the user already provided a reason, pass their exact text. "
        "Do not cite skills, file paths, or instructions. "
        "Never invent feedback. Cancel sends nothing and keeps routing on. "
        "Status checks delivery; retry resends the saved report without recapturing."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["on", "off", "cancel", "status", "retry"]},
            "reason": {"type": ["string", "null"]},
            "feedback_id": {"type": ["string", "null"]},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "deferLoading": False,
}


class CodexFeedbackInput:
    def __init__(self, send: Callable[[str], Awaitable[None]]):
        self.send = send
        self.pending: dict[str, tuple[str, str, asyncio.Future]] = {}
        self.canceled_turns: set[tuple[str, str]] = set()

    def consume(self, message: dict) -> bool:
        request_id = message.get("id")
        if (
            message.get("method") is not None
            or not isinstance(request_id, str)
            or not request_id.startswith("ug-feedback-input-")
        ):
            return False
        pending = self.pending.get(request_id)
        if pending is not None and not pending[2].done():
            pending[2].set_result(message)
        return True

    def cancel(self, thread_id: str, turn_id: str | None = None) -> None:
        if turn_id is not None:
            self.canceled_turns.add((thread_id, turn_id))
        for thread, turn, future in self.pending.values():
            if thread == thread_id and (turn_id is None or turn == turn_id) and not future.done():
                self.canceled_turns.add((thread, turn))
                future.set_result(None)

    async def ask(self, thread_id: str, turn_id: str, question: dict) -> list[str] | None:
        if (thread_id, turn_id) in self.canceled_turns:
            return None
        request_id = f"ug-feedback-input-{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = (thread_id, turn_id, future)
        try:
            await self.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": request_id,
                            "isBlocking": True,
                            "autoResolutionMs": None,
                            "questions": [question],
                        },
                    }
                )
            )
            reply = await future
            if reply is None or (thread_id, turn_id) in self.canceled_turns:
                return None
            result = reply.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise ValueError("Could not open the feedback prompt. Smart Router is still on.")
            answer = result["answers"].get(FEEDBACK_QUESTION_ID, {})
            values = answer.get("answers") if isinstance(answer, dict) else None
            if values is None:
                return None
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError("Invalid feedback response. Smart Router is still on.")
            return values
        finally:
            self.pending.pop(request_id, None)
            with contextlib.suppress(OSError, ConnectionClosed):
                await self.send(
                    json.dumps(
                        {
                            "method": "serverRequest/resolved",
                            "params": {"threadId": thread_id, "requestId": request_id},
                        }
                    )
                )


async def collect_feedback(ask: AskFeedback) -> str | None:
    question = {
        "id": FEEDBACK_QUESTION_ID,
        "header": "Smart Router",
        "question": f"Why are you turning Smart Router off? {FEEDBACK_DISCLOSURE}",
        "isOther": False,
        "isSecret": False,
        "options": [
            {"label": PROVIDE_REASON, "description": "Type your reason (at least 16 characters)."},
            {"label": CANCEL, "description": "Keep Smart Router on and send nothing."},
        ],
    }
    answer = await ask(question)
    if not answer or answer[0] == CANCEL:
        return None
    if answer[0] != PROVIDE_REASON:
        raise ValueError("Invalid feedback selection. Smart Router is still on.")
    notes = answer[1:]
    while True:
        if notes:
            if len(notes) != 1 or not notes[0].startswith("user_note: "):
                raise ValueError("Invalid feedback reason. Smart Router is still on.")
            reason = notes[0].removeprefix("user_note: ")
            if len(reason.strip()) >= 16:
                return reason
        question = {
            "id": FEEDBACK_QUESTION_ID,
            "header": "Your reason",
            "question": f"Please share at least 16 characters. {FEEDBACK_DISCLOSURE} Press Esc to cancel and keep routing on.",
            "isOther": False,
            "isSecret": False,
            "options": None,
        }
        notes = await ask(question)
        if not notes:
            return None


class CodexSmartRouter:
    def __init__(self, controls: Path):
        self.controls = controls
        self.environment = {**os.environ, SESSION_ENV_VAR: str(controls)}
        self.feedback_required = env_bool(ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR)
        self.pending_threads: set[str] = set()
        self.collecting_threads: set[str] = set()
        self.root_threads: set[str] = set()
        self.thread_requests: set[str | int] = set()
        self.uploads: dict[str, feedback.FeedbackUpload] = {}
        self.lock = asyncio.Lock()

    def before_tui(self, message: dict) -> None:
        method, params = message.get("method"), message.get("params")
        request_id = message.get("id")
        if (
            method not in ("thread/start", "thread/resume")
            or not isinstance(params, dict)
            or not isinstance(request_id, (str, int))
        ):
            return
        if method == "thread/start":
            tools = params.get("dynamicTools")
            if tools is not None and not isinstance(tools, list):
                return
            if any(
                isinstance(tool, dict) and tool.get("name") == TOOL_NAME for tool in tools or []
            ):
                return
            params["dynamicTools"] = [*(tools or []), TOOL_SPEC]
        self.thread_requests.add(request_id)

    def observe(self, message: dict) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, (str, int)) or request_id not in self.thread_requests:
            return
        self.thread_requests.remove(request_id)
        result = message.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return
        source = thread.get("source")
        if thread.get("parentThreadId") or (isinstance(source, dict) and "subAgent" in source):
            return
        if not isinstance(thread.get("id"), str):
            return
        try:
            self.root_threads.add(str(UUID(thread["id"])))
        except (ValueError, KeyError, TypeError):
            return

    def handles(self, message: dict) -> bool:
        params = message.get("params")
        return (
            message.get("method") == "item/tool/call"
            and isinstance(message.get("id"), (str, int))
            and isinstance(params, dict)
            and params.get("tool") == TOOL_NAME
            and params.get("namespace") in (None, "")
        )

    async def call(self, params: dict, ask: AskFeedback | None = None) -> dict:
        try:
            async with self.lock:
                arguments = params.get("arguments", {})
                thread_id = params.get("threadId")
                if (
                    isinstance(thread_id, str)
                    and thread_id in self.collecting_threads
                    and isinstance(arguments, dict)
                    and arguments.get("action") == "off"
                ):
                    raise ValueError("A Smart Router feedback prompt is already open.")
                supplied_reason = arguments.get("reason") if isinstance(arguments, dict) else None
                if (
                    self.feedback_required
                    and isinstance(params.get("threadId"), str)
                    and isinstance(arguments, dict)
                    and arguments.get("action") == "off"
                    and isinstance(supplied_reason, str)
                    and params.get("threadId") not in self.pending_threads
                ):
                    await asyncio.to_thread(
                        self.execute, {**params, "arguments": {**arguments, "reason": None}}
                    )
                result = await asyncio.to_thread(self.execute, params)
            if result.get("needs_reason") and ask is not None:
                thread_id = params["threadId"]
                self.collecting_threads.add(thread_id)
                try:
                    reason = await collect_feedback(ask)
                except BaseException:
                    self.pending_threads.discard(thread_id)
                    raise
                finally:
                    self.collecting_threads.discard(thread_id)
                arguments = (
                    {"action": "cancel"} if reason is None else {"action": "off", "reason": reason}
                )
                async with self.lock:
                    result = await asyncio.to_thread(
                        self.execute, {**params, "arguments": arguments}
                    )
            return {
                "contentItems": [{"type": "inputText", "text": json.dumps(result)}],
                "success": True,
            }
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "contentItems": [{"type": "inputText", "text": str(exc)}],
                "success": False,
            }

    def saved_report(self, report_id: str, thread_id: str) -> tuple[str, Path]:
        report_id = str(UUID(report_id))
        directory = Path.home() / ".isaac/feedback" / report_id
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("Feedback receipt is invalid.")
        target = receipt.get("target")
        request = receipt.get("request")
        if (
            not isinstance(target, dict)
            or target.get("session_id") != thread_id
            or not isinstance(request, dict)
            or request.get("harness") != "codex"
            or request.get("category") != "smart-routing"
            or receipt.get("feedback_id") != report_id
        ):
            raise ValueError("Feedback report does not belong to this native session.")
        return report_id, directory

    def delivery(self, report_id: str) -> dict[str, str]:
        result = feedback.codex_feedback_delivery(report_id)
        process = self.uploads.get(report_id)
        if process is None:
            return result
        if process.poll() is None:
            return {
                "delivery": "pending",
                "message": f"Feedback is sending in the background. You can keep working. Feedback ID: {report_id}",
                "feedback_id": report_id,
            }
        if result["delivery"] == "submitted":
            return result
        return {
            "delivery": "unconfirmed",
            "message": f"Feedback delivery is unconfirmed. Feedback ID: {report_id}. Ask Smart Router to retry this saved report.",
            "feedback_id": report_id,
        }

    def execute(self, params: dict) -> dict:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or thread_id not in self.root_threads:
            raise ValueError("Smart Router controls are available only to this session's root.")
        arguments = params.get("arguments")
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"action", "reason", "feedback_id"}
            or arguments.get("action") not in ("on", "off", "cancel", "status", "retry")
            or (arguments.get("reason") is not None and not isinstance(arguments["reason"], str))
            or (
                arguments.get("feedback_id") is not None
                and not isinstance(arguments["feedback_id"], str)
            )
            or (arguments.get("reason") is not None and arguments["action"] != "off")
            or (
                arguments.get("feedback_id") is not None
                and arguments["action"] not in ("status", "retry")
            )
        ):
            raise ValueError("Invalid Smart Router action.")
        action = arguments["action"]
        env = {**self.environment, "CODEX_THREAD_ID": thread_id, "CODEX_SESSION_ID": thread_id}
        if action in ("status", "retry"):
            report_id, directory = self.saved_report(arguments.get("feedback_id") or "", thread_id)
            if action == "retry":
                try:
                    executable = feedback._isaac()
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"Isaac feedback is unavailable. Report {report_id} is still saved; routing is unchanged."
                    ) from exc
                report = feedback.CapturedFeedback(executable, report_id, directory)
                self.uploads[report_id] = feedback.submit_codex_feedback(report, env)
            return self.delivery(report_id)
        enabled = session_routing_enabled(self.controls)
        if action == "cancel":
            self.pending_threads.discard(thread_id)
            return {"message": f"Canceled. Smart Router remains {'on' if enabled else 'off'}."}
        requested = action == "on"
        if enabled == requested:
            return {
                "message": f"Smart Router is already {'on' if enabled else 'off'} for this session."
            }
        report = None
        reason = arguments.get("reason")
        if not requested and self.feedback_required:
            if reason is None and thread_id not in self.pending_threads:
                feedback.prepare_codex_feedback(env)
                self.pending_threads.add(thread_id)
            if len((reason or "").strip()) < 16:
                return {
                    "needs_reason": True,
                    "message": (
                        "Why are you turning Smart Router off? Enter at least 16 characters. "
                        "Your reason and session details will be sent to the Isaac team. "
                        "You can cancel to keep routing on. Smart Router is still on."
                    ),
                }
            if thread_id not in self.pending_threads:
                raise ValueError(
                    "Call off without a reason first to anchor the preceding task. Smart Router is still on."
                )
            report = feedback.capture_codex_feedback(reason, env)
        set_session_environment(
            {} if requested else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), env=env
        )
        result: dict[str, Any] = {
            "message": f"Smart Router is {'on' if requested else 'off'} for this session."
        }
        self.pending_threads.discard(thread_id)
        if report is not None:
            result.update(feedback_id=report.feedback_id, notices=list(report.notices))
            try:
                self.uploads[report.feedback_id] = feedback.submit_codex_feedback(report, env)
                result.update(self.delivery(report.feedback_id))
                result["message"] = "Smart Router is off. " + result["message"]
            except (OSError, ValueError):
                result.update(
                    delivery="unconfirmed",
                    message="Smart Router is off. Feedback is saved but delivery could not start. Ask Smart Router to retry this saved report.",
                )
        return result
