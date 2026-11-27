"""Session-bound Smart Router tool executed by the UG launcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from websockets.exceptions import ConnectionClosed

from ucode.config_io import atomic_write_json
from ucode.constants import AGENT_CODEX, SMART_ROUTING_ENV_KEYS
from ucode.smart_routing import feedback, route_validation, routing, session_log
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    session_routing_enabled,
    set_session_environment,
    validation_dismissed_path,
    validation_result_path,
)

TOOL_NAME = "ug_smart_router"


class ThreadOwnership(Enum):
    CALLER = "caller"
    ROOT = "root"


PROVIDE_REASON = "Provide a reason"
CANCEL = "Cancel"
FEEDBACK_QUESTION_ID = "smart_router_feedback"
FEEDBACK_DISCLOSURE = "Your reason and session details will be sent to the Isaac team."
AskFeedback = Callable[[dict], Awaitable[list[str] | None]]
ValidateModelList = Callable[[], dict[str, Any]]
TOOL_SPEC = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Control Unity Gateway Smart Router for this root session at the user's request. "
        "Use this native tool instead of shell commands. Only call off for the user's explicit "
        "$smart-router off skill command, never for a natural-language request. "
        "Tell users to invoke that command if they request off in ordinary chat. "
        "Call off without a reason; "
        "if feedback is required, the launcher opens a native choice menu and collects the reason. "
        "The user can provide a reason (minimum 16 trimmed characters) or cancel. "
        "Do not ask another question or switch to Plan mode. "
        "Collect feedback only through the native prompt after its disclosure; never pass a reason yourself. "
        "If validation_error is returned, relay its message and stop. Wait for the user to retry; "
        "do not reopen the prompt or automatically retry with another reason. "
        "Do not cite skills, file paths, or instructions. "
        "Never invent feedback. Cancel sends nothing and keeps routing on."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["on", "off", "cancel"]}},
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
            answer = result["answers"].get(question["id"], {})
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
    if not notes:
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
    if len(notes) != 1 or not notes[0].startswith("user_note: "):
        raise ValueError("Invalid feedback reason. Smart Router is still on.")
    return notes[0].removeprefix("user_note: ")


class CodexSmartRouter:
    def __init__(self, controls: Path, validate: ValidateModelList | None = None):
        self.controls = controls
        self.validate = validate
        self.environment = {**os.environ, SESSION_ENV_VAR: str(controls)}
        self.feedback_required = feedback.smart_router_feedback_required()
        self.pending_feedback: dict[str, str | None] = {}
        self.collecting_threads: set[str] = set()
        self.threads: dict[str, ThreadOwnership] = {}
        self.thread_requests: dict[str | int, bool] = {}
        self.off_requests: dict[str | int, tuple[str, asyncio.Event]] = {}
        self.off_turns: set[tuple[str, str]] = set()
        self.uploads: dict[str, feedback.FeedbackUpload] = {}
        self.lock = asyncio.Lock()

    def before_tui(self, message: dict) -> None:
        method, params = message.get("method"), message.get("params")
        request_id = message.get("id")
        if (
            method == "turn/start"
            and isinstance(params, dict)
            and isinstance(request_id, (str, int))
        ):
            thread_id, inputs = params.get("threadId"), params.get("input")
            if isinstance(thread_id, str) and isinstance(inputs, list):
                texts = [
                    item.get("text")
                    for item in inputs
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                selected_skill = any(
                    isinstance(item, dict)
                    and item.get("type") == "skill"
                    and item.get("name") == "smart-router"
                    for item in inputs
                )
                if (
                    selected_skill
                    and len(texts) == 1
                    and isinstance(texts[0], str)
                    and texts[0].strip() in ("$smart-router off", "/smart-router off")
                ):
                    self.off_requests[request_id] = (thread_id, asyncio.Event())
            return
        if (
            method not in ("thread/start", "thread/resume")
            or not isinstance(params, dict)
            or not isinstance(request_id, (str, int))
        ):
            return
        tools = params.get("dynamicTools")
        if tools is not None and not isinstance(tools, list):
            return
        owns_tool = not any(
            isinstance(tool, dict) and tool.get("name") == TOOL_NAME for tool in tools or []
        )
        if method == "thread/start" and owns_tool:
            params["dynamicTools"] = [*(tools or []), TOOL_SPEC]
        self.thread_requests[request_id] = owns_tool

    def observe(self, message: dict) -> None:
        request_id = message.get("id")
        params = message.get("params")
        if message.get("method") == "turn/completed" and isinstance(params, dict):
            turn = params.get("turn")
            if (
                isinstance(turn, dict)
                and isinstance(params.get("threadId"), str)
                and isinstance(turn.get("id"), str)
            ):
                self.off_turns.discard((params["threadId"], turn["id"]))
        is_response = message.get("method") is None
        if is_response and isinstance(request_id, (str, int)) and request_id in self.off_requests:
            thread_id, ready = self.off_requests.pop(request_id)
            result = message.get("result")
            turn = result.get("turn") if isinstance(result, dict) else None
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self.off_turns.add((thread_id, turn["id"]))
            ready.set()
        if (
            not is_response
            or not isinstance(request_id, (str, int))
            or request_id not in self.thread_requests
        ):
            return
        owns_tool = self.thread_requests.pop(request_id)
        result = message.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            return
        try:
            thread_id = str(UUID(thread["id"]))
        except ValueError:
            return
        if not owns_tool:
            self.threads[thread_id] = ThreadOwnership.CALLER
            return
        source = thread.get("source")
        if (
            self.threads.get(thread_id) is ThreadOwnership.CALLER
            or thread.get("parentThreadId")
            or (isinstance(source, dict) and "subAgent" in source)
        ):
            return
        self.threads[thread_id] = ThreadOwnership.ROOT

    def handles(self, message: dict) -> bool:
        params = message.get("params")
        return (
            message.get("method") == "item/tool/call"
            and isinstance(message.get("id"), (str, int))
            and isinstance(params, dict)
            and isinstance(params.get("threadId"), str)
            and self.threads.get(params["threadId"]) is not ThreadOwnership.CALLER
            and isinstance(params.get("turnId"), str)
            and params.get("tool") == TOOL_NAME
            and params.get("namespace") in (None, "")
        )

    async def consume_off_command(self, params: dict) -> bool:
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return False
        pending = [
            ready.wait() for thread, ready in self.off_requests.values() if thread == thread_id
        ]
        if pending:
            try:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=5)
            except TimeoutError:
                return False
        key = (thread_id, turn_id)
        if key not in self.off_turns:
            return False
        self.off_turns.remove(key)
        return True

    async def call(self, params: dict, ask: AskFeedback | None = None) -> dict:
        try:
            arguments = params.get("arguments", {})
            if isinstance(arguments, dict) and arguments.get("action") == "off":
                if not await self.consume_off_command(params):
                    raise ValueError(
                        "Use $smart-router off to turn Smart Router off. Routing is unchanged."
                    )
                if arguments.get("reason") is not None:
                    raise ValueError(
                        "Enter your reason in the Smart Router feedback prompt. Routing is unchanged."
                    )
            async with self.lock:
                thread_id = params.get("threadId")
                if (
                    isinstance(thread_id, str)
                    and thread_id in self.collecting_threads
                    and isinstance(arguments, dict)
                    and arguments.get("action") == "off"
                ):
                    raise ValueError("A Smart Router feedback prompt is already open.")
                result = await asyncio.to_thread(self.execute, params)
            if result.get("needs_reason") and ask is not None:
                thread_id = params["threadId"]
                self.collecting_threads.add(thread_id)
                try:
                    reason = await collect_feedback(ask)
                except BaseException:
                    self.pending_feedback.pop(thread_id, None)
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

    async def delivery(self, report_id: str, process: feedback.FeedbackUpload) -> dict[str, str]:
        sent = await feedback.wait_for_feedback(process)
        message = (
            "Feedback submitted successfully." if sent else "Feedback delivery is unconfirmed."
        )
        return {
            "delivery": "submitted" if sent else "unconfirmed",
            "message": f"{message} Feedback ID: {report_id}",
        }

    def execute(self, params: dict) -> dict:
        thread_id = params.get("threadId")
        if (
            not isinstance(thread_id, str)
            or self.threads.get(thread_id) is not ThreadOwnership.ROOT
        ):
            raise ValueError("Smart Router controls are available only to this session's root.")
        arguments = params.get("arguments")
        if (
            not isinstance(arguments, dict)
            or set(arguments) - {"action", "reason"}
            or arguments.get("action") not in ("on", "off", "cancel")
            or (arguments.get("reason") is not None and not isinstance(arguments["reason"], str))
            or (arguments.get("reason") is not None and arguments["action"] != "off")
        ):
            raise ValueError("Invalid Smart Router action.")
        action = arguments["action"]
        env = {**self.environment, "CODEX_THREAD_ID": thread_id, "CODEX_SESSION_ID": thread_id}
        enabled = session_routing_enabled(self.controls)
        if action == "cancel":
            self.pending_feedback.pop(thread_id, None)
            return {"message": f"Canceled. Smart Router remains {'on' if enabled else 'off'}."}
        requested = action == "on"
        if requested:
            verdict = self._check_model_list(thread_id)
            if verdict["routing_disabled"]:
                set_session_environment(dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), env=env)
                return {"message": "Smart Router is off for this session."}
        if enabled == requested:
            self._clear_validation()
            return {
                "message": f"Smart Router is already {'on' if enabled else 'off'} for this session."
            }
        result = {}
        if not requested and self.feedback_required:
            reason = arguments.get("reason")
            if reason is None:
                self.pending_feedback[thread_id] = feedback.prepare_codex_feedback(env)
                return {"needs_reason": True}
            if len(reason.strip()) < 16:
                self.pending_feedback.pop(thread_id, None)
                return {
                    "validation_error": "reason_too_short",
                    "message": (
                        f"Reason too short ({len(reason.strip())}/16 characters). "
                        "Smart Router is still on; nothing was sent. Run $smart-router off again to retry."
                    ),
                }
            if thread_id not in self.pending_feedback:
                raise ValueError(
                    "Open the Smart Router feedback prompt first. Smart Router is still on."
                )
            turn_id = self.pending_feedback.pop(thread_id)
            report_id = str(uuid4())
            try:
                self.uploads[report_id] = feedback.submit_codex_feedback(
                    report_id, env, reason=reason, turn_id=turn_id
                )
                result = self.sending(report_id)
                if turn_id is None:
                    result["notices"] = [
                        "No earlier task was found; this feedback has no task or model request to analyze."
                    ]
            except (OSError, RuntimeError):
                result = {
                    "delivery": "unconfirmed",
                    "message": "Feedback delivery could not start.",
                }
        try:
            set_session_environment(
                {} if requested else dict.fromkeys(SMART_ROUTING_ENV_KEYS, "0"), env=env
            )
        except RuntimeError:
            if not result:
                raise
            result["message"] = (
                f"Could not turn Smart Router off. {result['message']} Routing is unchanged."
            )
            return result
        self.pending_feedback.pop(thread_id, None)
        self._clear_validation()
        message = f"Smart Router is {'on' if requested else 'off'} for this session."
        return {**result, "message": message + (" " + result["message"] if result else "")}

    def _check_model_list(self, thread_id: str) -> dict[str, Any]:
        verdict = self._model_list_verdict()
        if verdict["routing_disabled"]:
            self._publish_verdict(thread_id, verdict)
        return verdict

    def _model_list_verdict(self) -> dict[str, Any]:
        if self.validate is None:
            return {"routing_disabled": False}
        started = time.monotonic()
        try:
            return self.validate()
        except Exception:
            return route_validation.check_failed(routing.configured_router_name(), started)

    def _publish_verdict(self, thread_id: str, verdict: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            validation_dismissed_path(self.controls).unlink(missing_ok=True)
        with contextlib.suppress(OSError, RuntimeError):
            atomic_write_json(validation_result_path(self.controls), {**verdict, "logged": True})
        with contextlib.suppress(OSError, ValueError):
            session_log.append_route_validation(AGENT_CODEX, thread_id, verdict)

    def _clear_validation(self) -> None:
        for path in (
            validation_result_path(self.controls),
            validation_dismissed_path(self.controls),
        ):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    @staticmethod
    def sending(report_id: str) -> dict:
        return {
            "delivery": "pending",
            "feedback_id": report_id,
            "message": f"Sending feedback in the background. You can keep working. Feedback ID: {report_id}",
        }
