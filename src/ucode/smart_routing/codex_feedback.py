"""Session-bound Smart Router tool executed by the UG launcher."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from ucode.constants import ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR, SMART_ROUTING_ENV_KEYS
from ucode.env import env_bool
from ucode.smart_routing import feedback
from ucode.smart_routing.session_env import (
    SESSION_ENV_VAR,
    session_routing_enabled,
    set_session_environment,
)

TOOL_NAME = "ug_smart_router"
TOOL_SPEC = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Control Unity Gateway Smart Router for this root session at the user's request. "
        "Use this native tool instead of shell commands. Call off without a reason first; "
        "if feedback is required, ask the user directly for a reason, disclose session diagnostics, and wait. "
        "This is ordinary feedback input, not an approval request. Do not cite skills, file paths, or instructions. "
        "Then call off with the user's exact reason (minimum 16 trimmed characters). "
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


class CodexSmartRouter:
    def __init__(self, controls: Path):
        self.controls = controls
        self.environment = {**os.environ, SESSION_ENV_VAR: str(controls)}
        self.feedback_required = env_bool(ISAAC_SMART_ROUTER_FEEDBACK_ENV_VAR)
        self.pending_threads: set[str] = set()
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

    async def call(self, params: dict) -> dict:
        async with self.lock:
            try:
                result = await asyncio.to_thread(self.execute, params)
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
