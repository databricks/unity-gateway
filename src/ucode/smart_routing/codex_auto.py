"""Launch-local Auto selection for the native Codex model menu."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from ucode.config_io import atomic_write_json
from ucode.smart_routing.codex_feedback import (
    CodexFeedbackInput,
    CodexSmartRouter,
    ThreadOwnership,
    collect_feedback,
)
from ucode.smart_routing.session_env import (
    auto_state_path,
    session_routing_enabled,
    set_session_environment,
)

AUTO_MODEL = "codex-auto-ug"


class CodexAutoPicker:
    def __init__(
        self,
        router: CodexSmartRouter,
        inputs: CodexFeedbackInput,
        send: Callable[[str], Awaitable[None]],
        notify: Callable[[str], Awaitable[None]],
    ):
        self.router = router
        self.inputs = inputs
        self.send = send
        self.notify = notify
        self.models: dict[str, dict] = {}
        self.requests: dict[str | int, str] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.selections: dict[str | int, str] = {}
        self.settings: dict = {}
        self.thread_id: str | None = None
        self.turn_id = ""
        self.busy = False
        self.saved_model: str | None = None
        self.upload_tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return session_routing_enabled(self.router.controls)

    async def ui_enabled(self) -> bool:
        try:
            return await asyncio.to_thread(session_routing_enabled, self.router.controls)
        except (OSError, ValueError):
            return False

    @property
    def main_model(self) -> str | None:
        return self.settings.get("model")

    async def request(self, method: str, params: dict) -> None:
        request_id = f"ug-auto-{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(json.dumps({"id": request_id, "method": method, "params": params}))
            response = await asyncio.wait_for(future, timeout=10)
            if "error" in response:
                raise ValueError("Codex rejected the model change. Reopen /model to retry.")
        finally:
            self.pending.pop(request_id, None)

    async def before_tui(self, message: dict) -> bool:
        """Return True when a request was rejected locally."""
        method, params = message.get("method"), message.get("params")
        request_id = message.get("id")
        if not isinstance(params, dict) or not isinstance(request_id, (str, int)):
            return False
        if method in ("model/list", "thread/start", "thread/resume"):
            if method == "model/list" and params.get("cursor") is None:
                self.models.clear()
            self.requests[request_id] = method
        if method == "turn/start" and params.get("threadId") == self.thread_id:
            if self.busy:
                await self.notify(
                    json.dumps(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32600,
                                "message": "Finish or cancel the Auto model picker first.",
                            },
                        }
                    )
                )
                return True
        if method == "thread/settings/update" and params.get("threadId") == self.thread_id:
            model = params.get("model")
            if isinstance(model, str) and self.main_model and not self.busy:
                if model == AUTO_MODEL or model in self.models:
                    self.busy = True
                    self.selections[request_id] = model
                    # Accept the native menu action before opening a prompt: the TUI awaits this RPC.
                    params["model"] = self.main_model
                    params.pop("effort", None)
                    params.pop("collaborationMode", None)
            elif self.busy:
                params.pop("effort", None)
        if params.get("model") == AUTO_MODEL:
            if not self.main_model:
                await self.notify(
                    json.dumps(
                        {
                            "id": request_id,
                            "error": {
                                "code": -32602,
                                "message": "Auto needs an active main model. Select a model first.",
                            },
                        }
                    )
                )
                return True
            params["model"] = self.main_model
        collaboration = params.get("collaborationMode")
        if isinstance(collaboration, dict):
            settings = collaboration.get("settings")
            if isinstance(settings, dict) and settings.get("model") == AUTO_MODEL:
                settings["model"] = self.main_model
        # Native /model also saves defaults. Never persist the synthetic menu ID.
        edits = params.get("edits", []) if method == "config/batchWrite" else []
        if method == "config/value/write":
            edits = [params]
        for edit in edits:
            if isinstance(edit, dict):
                if edit.get("keyPath") == "model" and (
                    edit.get("value") == AUTO_MODEL or self.busy
                ):
                    edit["value"] = self.main_model
                if edit.get("keyPath") == "model_reasoning_effort" and self.busy:
                    edit["value"] = self.settings.get("effort")
        return False

    async def observe(self, message: dict) -> tuple[bool, str | None]:
        """Consume private replies; otherwise annotate native models/settings in place."""
        request_id = message.get("id")
        if (
            message.get("method") is None
            and isinstance(request_id, str)
            and request_id.startswith("ug-auto-")
        ):
            future = self.pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)
            return True, None
        method = self.requests.pop(request_id, None) if message.get("method") is None else None
        result = message.get("result")
        if method == "model/list" and isinstance(result, dict):
            rows = result.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if (
                        isinstance(row, dict)
                        and isinstance(row.get("model"), str)
                        and not row.get("hidden")
                        and row["model"] != AUTO_MODEL
                        and isinstance(row.get("defaultReasoningEffort"), str)
                        and isinstance(row.get("supportedReasoningEfforts"), list)
                        and all(
                            isinstance(option, dict)
                            and isinstance(option.get("reasoningEffort"), str)
                            for option in row["supportedReasoningEfforts"]
                        )
                    ):
                        self.models[row["model"]] = row
                if self.models and not any(
                    isinstance(row, dict) and row.get("model") == AUTO_MODEL for row in rows
                ):
                    base = self.models.get(self.main_model) or next(iter(self.models.values()))
                    result["data"] = [
                        {
                            **base,
                            "id": AUTO_MODEL,
                            "model": AUTO_MODEL,
                            "displayName": "Auto",
                            "description": "Choose your main model; smart-route subagents.",
                            "isDefault": False,
                            "supportedReasoningEfforts": [
                                option
                                for option in base["supportedReasoningEfforts"]
                                if option["reasoningEffort"] == base["defaultReasoningEffort"]
                            ],
                            "defaultReasoningEffort": base["defaultReasoningEffort"],
                        },
                        *rows,
                    ]
        if method in ("thread/start", "thread/resume") and isinstance(result, dict):
            thread = result.get("thread", {})
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if self.router.threads.get(thread_id) is ThreadOwnership.ROOT:
                self.thread_id = thread_id
                self.settings = {
                    "model": result.get("model"),
                    "effort": result.get("reasoningEffort"),
                }
                if await self.ui_enabled():
                    result["model"] = AUTO_MODEL
        params = message.get("params")
        if isinstance(params, dict) and params.get("threadId") == self.thread_id:
            if message.get("method") == "turn/started":
                turn = params.get("turn")
                self.turn_id = turn.get("id", "") if isinstance(turn, dict) else ""
            if message.get("method") == "thread/settings/updated":
                settings = params.get("threadSettings")
                if isinstance(settings, dict):
                    self.settings = settings.copy()
                    if await self.ui_enabled():
                        settings["model"] = AUTO_MODEL
        if (
            self.main_model
            and self.main_model != self.saved_model
            and (
                method in ("thread/start", "thread/resume")
                or message.get("method") == "thread/settings/updated"
            )
        ):
            try:
                await asyncio.to_thread(
                    atomic_write_json,
                    auto_state_path(self.router.controls),
                    {"model": self.main_model},
                )
                self.saved_model = self.main_model
            except (OSError, RuntimeError) as exc:
                logging.getLogger(__name__).warning(
                    "Could not update Codex Auto model state: %s", exc
                )
        selection = self.selections.pop(request_id, None) if message.get("method") is None else None
        if selection and "error" in message:
            self.busy = False
            selection = None
        return False, selection

    async def ask(self, question: dict) -> list[str] | None:
        assert self.thread_id is not None
        # Picker prompts are independent of inference turns and must also work before the first prompt.
        return await self.inputs.ask(self.thread_id, "", question)

    async def route(self, action: str, reason: str | None = None) -> dict:
        arguments = {"action": action}
        if reason is not None:
            arguments["reason"] = reason
        async with self.router.lock:
            return await asyncio.to_thread(
                self.router.execute, {"threadId": self.thread_id, "arguments": arguments}
            )

    async def report_delivery(self, report_id: str) -> None:
        process = self.router.uploads.pop(report_id, None)
        if process is None:
            return
        try:
            result = await self.router.delivery(report_id, process)
            message = result["message"]
        except (OSError, RuntimeError, ValueError):
            message = f"Feedback delivery is unconfirmed. Feedback ID: {report_id}."
        try:
            await self.notify(
                json.dumps(
                    {
                        "method": "warning",
                        "params": {"threadId": self.thread_id, "message": message},
                    }
                )
            )
        except (OSError, RuntimeError, ConnectionClosed):
            pass

    async def choose(self, selection: str) -> None:
        old_settings = self.settings.copy()
        old_controls = None
        try:
            parsed_controls = json.loads(await asyncio.to_thread(self.router.controls.read_text))
            if not isinstance(parsed_controls, dict):
                raise ValueError("Invalid Smart Router session controls.")
            old_controls = parsed_controls
            model = selection
            if selection == AUTO_MODEL:
                ordered = sorted(self.models, key=lambda name: name != self.main_model)
                answer = await self.ask(
                    {
                        "id": "auto_main_model",
                        "header": "Main model",
                        "question": "Choose the main model for Auto. Subagents will be smart-routed.",
                        "isOther": False,
                        "isSecret": False,
                        "options": [
                            {"label": name, "description": self.models[name].get("description", "")}
                            for name in ordered
                        ],
                    }
                )
                if not answer:
                    return
                model = answer[0]
                if len(answer) != 1 or model not in self.models:
                    raise ValueError("Select a model from the offered Auto catalog.")
                if self.turn_id and model != self.main_model:
                    answer = await self.ask(
                        {
                            "id": "auto_confirm",
                            "header": "Switch model",
                            "question": "Changing the main model can lose prompt-cache reuse. Continue?",
                            "isOther": False,
                            "isSecret": False,
                            "options": [
                                {
                                    "label": "Switch model",
                                    "description": f"Use {model} for the main agent.",
                                },
                                {
                                    "label": "Cancel",
                                    "description": "Keep the current main model and routing state.",
                                },
                            ],
                        }
                    )
                    if answer != ["Switch model"]:
                        return
            row = self.models.get(model)
            if row is None:
                raise ValueError("The selected model is no longer available. Reopen /model.")
            result = await self.route("on" if selection == AUTO_MODEL else "off")
            if result.get("needs_reason"):
                reason = await collect_feedback(self.ask)
                if reason is None:
                    await self.route("cancel")
                    return
                result = await self.route("off", reason)
                if result.get("validation_error"):
                    raise ValueError(result["message"])
            if self.enabled != (selection == AUTO_MODEL):
                raise ValueError(result.get("message", "Could not change Smart Router state."))
            if result.get("delivery") == "pending":
                task = asyncio.create_task(self.report_delivery(result["feedback_id"]))
                self.upload_tasks.add(task)
                task.add_done_callback(self.upload_tasks.discard)
            effort = old_settings.get("effort")
            if effort not in {
                option["reasoningEffort"] for option in row.get("supportedReasoningEfforts", [])
            }:
                effort = row["defaultReasoningEffort"]
            await self.request(
                "thread/settings/update",
                {"threadId": self.thread_id, "model": model, "effort": effort},
            )
            await self.request(
                "config/batchWrite",
                {
                    "edits": [
                        {"keyPath": "model", "value": model, "mergeStrategy": "replace"},
                        {
                            "keyPath": "model_reasoning_effort",
                            "value": effort,
                            "mergeStrategy": "replace",
                        },
                    ]
                },
            )
            self.settings.update(model=model, effort=effort)
        except (OSError, RuntimeError, ValueError, TimeoutError, KeyError) as exc:
            # Restore the accepted model and routing state if a later step fails.
            message = str(exc)
            if old_controls is not None:
                try:
                    await asyncio.to_thread(
                        set_session_environment, old_controls, env=self.router.environment
                    )
                    await self.request(
                        "thread/settings/update",
                        {
                            "threadId": self.thread_id,
                            "model": old_settings["model"],
                            "effort": old_settings.get("effort"),
                        },
                    )
                except (OSError, RuntimeError, ValueError, TimeoutError, KeyError) as rollback:
                    message += f" Could not restore the previous selection: {rollback}"
            self.settings = old_settings
            await self.notify(
                json.dumps(
                    {
                        "method": "warning",
                        "params": {"threadId": self.thread_id, "message": message},
                    }
                )
            )
        finally:
            self.busy = False
            settings = self.settings.copy()
            try:
                if self.enabled:
                    settings["model"] = AUTO_MODEL
            except (OSError, ValueError):
                pass
            if {
                "approvalPolicy",
                "approvalsReviewer",
                "collaborationMode",
                "cwd",
                "modelProvider",
                "sandboxPolicy",
            } <= settings.keys():
                await self.notify(
                    json.dumps(
                        {
                            "method": "thread/settings/updated",
                            "params": {"threadId": self.thread_id, "threadSettings": settings},
                        }
                    )
                )
