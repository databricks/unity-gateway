from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from ucode.smart_routing import codex_routing, routing
from ucode.smart_routing.codex_auto import CodexAutoPicker
from ucode.smart_routing.codex_feedback import (
    CodexFeedbackInput,
    CodexSmartRouter,
    ValidateModelList,
)
from ucode.smart_routing.codex_metadata import RoutingMetadata
from ucode.smart_routing.feedback import FeedbackUpload

SETTINGS_UPDATED = "thread/settings/updated"
SETTINGS_UPDATE = "thread/settings/update"
ITEM_STARTED = "item/started"
ITEM_COMPLETED = "item/completed"
TURN_START = "turn/start"
TURN_STARTED = "turn/started"

RouteDecisionFn = Callable[[str], tuple[routing.RoutingDecision | None, str | None]]
SwitchMessageFn = Callable[[str, str], str]
TokenProvider = Callable[[], str]


def _prompt_from_turn(params: dict) -> str | None:
    """Extract the plaintext portions of a Codex ``turn/start`` input."""
    raw_input = params.get("input")
    if isinstance(raw_input, str):
        return raw_input if raw_input.strip() else None
    if not isinstance(raw_input, list):
        return None

    parts: list[str] = []
    for item in raw_input:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    prompt = "\n".join(part for part in parts if part.strip())
    return prompt or None


@dataclass
class TuiFrameResult:
    frame: str
    needs_settings_update: bool


class _Session:
    def __init__(
        self,
        target_model: str | None,
        log: Callable[[str], None],
        switch_message: str | None = None,
        available_models: list[str] | None = None,
        route_decision: RouteDecisionFn | None = None,
        switch_message_fn: SwitchMessageFn | None = None,
    ) -> None:
        self.target = target_model
        self.available_models = list(available_models or [])
        self.log = log
        self.switch_message = switch_message
        self.route_decision = route_decision
        self.switch_message_fn = switch_message_fn
        self.thread_id: str | None = None
        self.settings: dict | None = None
        self.first_turn_seen = False
        self.switch_pending = False
        self.notice_pending = False
        self.injected = False
        self.settings_update_id: str | None = None
        self.settings_update_event: asyncio.Event = asyncio.Event()

    def on_tui_frame(self, raw: str) -> TuiFrameResult:
        try:
            msg = json.loads(raw)
        except ValueError:
            return TuiFrameResult(raw, needs_settings_update=False)
        if not isinstance(msg, dict):
            return TuiFrameResult(raw, needs_settings_update=False)
        params = msg.get("params")
        if msg.get("method") == TURN_START and isinstance(params, dict):
            if isinstance(params.get("threadId"), str):
                self.thread_id = params["threadId"]
            if self.first_turn_seen:
                return TuiFrameResult(raw, needs_settings_update=False)
            self.first_turn_seen = True
            if self.route_decision is not None:
                prompt = _prompt_from_turn(params)
                if prompt is None:
                    self.log("[ROUTE] first turn had no plaintext prompt; keeping current model")
                    return TuiFrameResult(raw, needs_settings_update=False)
                decision, reason = self.route_decision(prompt)
                if decision is None:
                    self.log(f"[ROUTE] selection failed; keeping current model: {reason}")
                    return TuiFrameResult(raw, needs_settings_update=False)
                decision = replace(decision, model=codex_routing.codex_model_id(decision.model))
                self.target = decision.model
                if self.switch_message_fn is not None:
                    self.switch_message = self.switch_message_fn(decision.model, decision.rationale)
                self.notice_pending = self.switch_message is not None
                self.log(f"[ROUTE] selected {decision.model!r}; rationale={decision.rationale!r}")
            old = params.get("model")
            if self.target is not None and old != self.target:
                params["model"] = self.target
                collab = params.get("collaborationMode")
                if isinstance(collab, dict):
                    settings = collab.get("settings")
                    if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                        settings["model"] = self.target
                self.switch_pending = True
                self.log(f"[REWRITE] model {old!r} -> {self.target!r}")
                return TuiFrameResult(json.dumps(msg), needs_settings_update=True)
        return TuiFrameResult(raw, needs_settings_update=False)

    def on_engine_frame(self, raw: str) -> list[dict]:
        try:
            msg = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(msg, dict):
            return []
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        for src in (params, result):
            thread = src.get("thread")
            tid = src.get("threadId") or (thread.get("id") if isinstance(thread, dict) else None)
            if isinstance(tid, str):
                self.thread_id = tid
            ts = src.get("threadSettings")
            if isinstance(ts, dict):
                self.settings = ts
        if (
            self.settings_update_id is not None
            and isinstance(msg.get("id"), str)
            and msg["id"] == self.settings_update_id
        ):
            self.settings_update_id = None
            self.settings_update_event.set()
        if (
            msg.get("method") == TURN_STARTED
            and not self.injected
            and (self.switch_pending or self.notice_pending)
            and self.thread_id
        ):
            self.injected = True
            switch_pending = self.switch_pending
            self.switch_pending = False
            self.notice_pending = False
            injected: list[dict] = []
            if switch_pending:
                settings = dict(self.settings) if isinstance(self.settings, dict) else {}
                settings["model"] = self.target
                self.log(f"[INJECT] {SETTINGS_UPDATED}: model -> {self.target!r} (flip TUI chip)")
                injected.append(
                    {
                        "method": SETTINGS_UPDATED,
                        "params": {"threadId": self.thread_id, "threadSettings": settings},
                    }
                )
            if self.switch_message:
                turn = params.get("turn")
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                now_ms = int(time.time() * 1000)
                item = {
                    "type": "agentMessage",
                    "id": f"ucode-smart-router-{uuid.uuid4().hex}",
                    "text": self.switch_message,
                    "phase": None,
                    "memoryCitation": None,
                }
                self.log(f"[INJECT] agentMessage note (started+completed): {self.switch_message!r}")
                injected.append(
                    {
                        "method": ITEM_STARTED,
                        "params": {
                            "item": item,
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "startedAtMs": now_ms,
                        },
                    }
                )
                injected.append(
                    {
                        "method": ITEM_COMPLETED,
                        "params": {
                            "item": item,
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "completedAtMs": now_ms,
                        },
                    }
                )
            return injected
        return []


async def _handle_tui(
    tui,
    upstream_uri: str,
    target_model: str | None,
    log,
    switch_message: str | None = None,
    available_models: list[str] | None = None,
    workspace: str | None = None,
    token_provider: TokenProvider | None = None,
    switch_message_fn: SwitchMessageFn | None = None,
    session_controls: Path | None = None,
    provider: str | None = None,
    auto_picker: bool = False,
    validate_model_list: ValidateModelList | None = None,
) -> None:
    path = getattr(getattr(tui, "request", None), "path", "/") or "/"
    uri = upstream_uri.rstrip("/") + path
    log(f"[CONN] TUI connected (path={path}); dialing app-server {uri}")
    route_decision: RouteDecisionFn | None = None
    if workspace is not None and token_provider is not None:

        def route_decision(prompt: str):
            try:
                token = token_provider()
            except RuntimeError as exc:
                return None, f"could not refresh workspace auth: {exc}"
            return codex_routing.request_routing_decision(
                workspace,
                token,
                prompt,
                list(available_models or []),
                log=log,
                **({"provider": provider} if provider else {}),
            )

    sess = _Session(
        target_model,
        log,
        switch_message,
        available_models,
        route_decision,
        switch_message_fn,
    )
    async with connect(uri, max_size=None) as upstream:
        controller = (
            RoutingMetadata(session_controls, log) if session_controls is not None else None
        )
        smart_router = (
            CodexSmartRouter(session_controls, validate_model_list)
            if session_controls is not None
            else None
        )
        feedback_input = CodexFeedbackInput(tui.send)
        picker = (
            CodexAutoPicker(smart_router, feedback_input, upstream.send, tui.send)
            if auto_picker and smart_router is not None
            else None
        )
        tool_tasks: set[asyncio.Task] = set()
        tool_turns: dict[tuple[str, str], asyncio.Event] = {}
        queued_frames: asyncio.Queue = asyncio.Queue()

        def parse(frame) -> dict | None:
            if not isinstance(frame, str):
                return None
            try:
                message = json.loads(frame)
            except ValueError:
                return None
            return message if isinstance(message, dict) else None

        async def read_tui():
            assert controller is not None
            async for frame in tui:
                message = parse(frame)
                if message is not None and feedback_input.consume(message):
                    continue
                if message is not None and message.get("method") == "turn/interrupt":
                    params = message.get("params")
                    if (
                        isinstance(params, dict)
                        and isinstance(params.get("threadId"), str)
                        and (params.get("turnId") is None or isinstance(params.get("turnId"), str))
                    ):
                        turn_id = params.get("turnId")
                        if (
                            picker is not None
                            and picker.busy
                            and params["threadId"] == picker.thread_id
                        ):
                            # Auto prompts have private turn IDs, not the TUI's last inference ID.
                            turn_id = None
                        feedback_input.cancel(params["threadId"], turn_id)
                if message is not None and message.get("method") in (None, "turn/interrupt"):
                    await upstream.send(frame)
                else:
                    await queued_frames.put(frame)
            await queued_frames.put(None)

        async def tui_frames():
            if controller is None:
                async for frame in tui:
                    yield frame
                return
            while (frame := await queued_frames.get()) is not None:
                yield frame

        async def tui_to_app():
            async for frame in tui_frames():
                if isinstance(frame, str):
                    message = parse(frame)
                    if controller is not None and message is not None:
                        assert smart_router is not None
                        if picker is not None and await picker.before_tui(message):
                            continue
                        smart_router.before_tui(message)
                        message = await controller.before_tui(message)
                        frame = json.dumps(message)
                    result = await asyncio.to_thread(sess.on_tui_frame, frame)
                    if result.needs_settings_update and sess.thread_id:
                        update_id = f"ucode-route-{uuid.uuid4().hex}"
                        update_req = json.dumps(
                            {
                                "id": update_id,
                                "method": SETTINGS_UPDATE,
                                "params": {
                                    "threadId": sess.thread_id,
                                    "model": sess.target,
                                },
                            }
                        )
                        sess.settings_update_id = update_id
                        log(
                            f"[ROUTE] sending {SETTINGS_UPDATE} model={sess.target!r} to app-server"
                        )
                        await upstream.send(update_req)
                        try:
                            await asyncio.wait_for(sess.settings_update_event.wait(), timeout=10)
                            log("[ROUTE] settings/update confirmed by app-server")
                        except TimeoutError:
                            log("[ROUTE] settings/update timed out; forwarding turn/start anyway")
                    await upstream.send(result.frame)
                else:
                    await upstream.send(frame)

        async def run_router_tool(message: dict, turn_done: asyncio.Event):
            assert smart_router is not None
            params = message["params"]

            async def ask(question: dict):
                return await feedback_input.ask(params["threadId"], params["turnId"], question)

            response = await smart_router.call(params, ask)
            result = json.loads(response["contentItems"][0]["text"]) if response["success"] else {}
            report_id = result.get("feedback_id", "")
            process = (
                smart_router.uploads.pop(report_id) if result.get("delivery") == "pending" else None
            )
            await upstream.send(json.dumps({"id": message["id"], "result": response}))
            if process is not None:
                await monitor_delivery(message, report_id, process, turn_done)

        async def monitor_delivery(
            message: dict, report_id: str, process: FeedbackUpload, turn_done: asyncio.Event
        ):
            assert smart_router is not None
            try:
                status = await smart_router.delivery(report_id, process)
            except (OSError, ValueError):
                status = {
                    "message": f"Feedback delivery is unconfirmed. Feedback ID: {report_id}.",
                }
            await delivery_notice(message, status["message"], turn_done)

        async def delivery_notice(message: dict, text: str, turn_done: asyncio.Event):
            await turn_done.wait()
            item = {
                "type": "agentMessage",
                "id": f"ug-feedback-{uuid.uuid4().hex}",
                "text": text,
                "phase": None,
                "memoryCitation": None,
            }
            for method in (ITEM_STARTED, ITEM_COMPLETED):
                timestamp_key = "startedAtMs" if method == ITEM_STARTED else "completedAtMs"
                await tui.send(
                    json.dumps(
                        {
                            "method": method,
                            "params": {
                                "item": item,
                                "threadId": message["params"]["threadId"],
                                "turnId": message["params"]["turnId"],
                                timestamp_key: int(time.time() * 1000),
                            },
                        }
                    )
                )

        def tool_done(task: asyncio.Task):
            tool_tasks.discard(task)
            if not task.cancelled() and (exc := task.exception()) is not None:
                log(f"[ERR] Smart Router tool: {type(exc).__name__}")

        async def app_to_tui():
            async for frame in upstream:
                message = parse(frame)
                if smart_router is not None and message is not None:
                    smart_router.observe(message)
                    if smart_router.handles(message):
                        key = (message["params"]["threadId"], message["params"]["turnId"])
                        turn_done = tool_turns.setdefault(key, asyncio.Event())
                        task = asyncio.create_task(run_router_tool(message, turn_done))
                        tool_tasks.add(task)
                        task.add_done_callback(tool_done)
                        continue
                if controller is not None and message is not None:
                    controller.observe(message)
                selection = None
                if picker is not None and message is not None:
                    consumed, selection = await picker.observe(message)
                    if consumed:
                        continue
                    frame = json.dumps(message)
                await tui.send(frame)
                if selection is not None and picker is not None:
                    task = asyncio.create_task(picker.choose(selection))
                    tool_tasks.add(task)
                    task.add_done_callback(tool_done)
                if message is not None and message.get("method") == "turn/completed":
                    params = message.get("params")
                    turn = params.get("turn") if isinstance(params, dict) else None
                    if (
                        isinstance(params, dict)
                        and isinstance(params.get("threadId"), str)
                        and isinstance(turn, dict)
                        and isinstance(turn.get("id"), str)
                    ):
                        key = (params["threadId"], turn["id"])
                        turn_done = tool_turns.pop(key, None)
                        if turn_done is not None:
                            feedback_input.cancel(*key)
                            turn_done.set()
                        feedback_input.canceled_turns.discard(key)
                if isinstance(frame, str):
                    for inj in sess.on_engine_frame(frame):
                        await tui.send(json.dumps(inj))

        a = asyncio.create_task(tui_to_app())
        b = asyncio.create_task(app_to_tui())
        tasks = {a, b}
        if controller is not None:
            tasks.add(asyncio.create_task(read_tui()))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tool_tasks:
                task.cancel()
            await asyncio.gather(*tool_tasks, return_exceptions=True)
            if picker is not None:
                upload_tasks = list(picker.upload_tasks)
                for task in upload_tasks:
                    task.cancel()
                await asyncio.gather(*upload_tasks, return_exceptions=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    log("[CONN] TUI session closed")


async def _serve(
    host: str,
    port: int,
    upstream_uri: str,
    model: str | None,
    log,
    switch_message: str | None = None,
    available_models: list[str] | None = None,
    workspace: str | None = None,
    token_provider: TokenProvider | None = None,
    switch_message_fn: SwitchMessageFn | None = None,
    session_controls: Path | None = None,
    provider: str | None = None,
    auto_picker: bool = False,
    validate_model_list: ValidateModelList | None = None,
):
    async def handler(tui):
        try:
            await _handle_tui(
                tui,
                upstream_uri,
                model,
                log,
                switch_message,
                available_models,
                workspace,
                token_provider,
                switch_message_fn,
                session_controls,
                provider,
                auto_picker,
                validate_model_list,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"[ERR] session: {exc!r}")

    server = await serve(handler, host, port, max_size=None)
    bound_port = server.sockets[0].getsockname()[1]
    log(f"[READY] ws://{host}:{bound_port} -> {upstream_uri} (switch -> {model!r})")
    return server


def start_interposer_thread(
    host: str,
    upstream_uri: str,
    model: str | None = None,
    *,
    available_models: list[str] | None = None,
    workspace: str | None = None,
    token_provider: TokenProvider | None = None,
    switch_message_fn: SwitchMessageFn | None = None,
    switch_message: str | None = None,
    log_path: Path | None = None,
    ready_timeout: float = 10.0,
    session_controls: Path | None = None,
    provider: str | None = None,
    auto_picker: bool = False,
    validate_model_list: ValidateModelList | None = None,
) -> tuple[int, Callable[[], None]]:
    def log(message: str) -> None:
        if log_path is None:
            return
        line = f"{time.strftime('%H:%M:%S')} {message}\n"
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    loop = asyncio.new_event_loop()
    holder: dict = {}
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            holder["server"] = loop.run_until_complete(
                _serve(
                    host,
                    0,
                    upstream_uri,
                    model,
                    log,
                    switch_message,
                    available_models,
                    workspace,
                    token_provider,
                    switch_message_fn,
                    session_controls,
                    provider,
                    auto_picker,
                    validate_model_list,
                )
            )
            holder["port"] = holder["server"].sockets[0].getsockname()[1]
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc
            log(f"[ERR] failed to start interposer: {exc!r}")
            ready.set()
            loop.close()
            return
        ready.set()
        loop.run_forever()
        server = holder.get("server")
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                loop.run_until_complete(server.wait_closed())
        loop.close()

    thread = threading.Thread(target=run, name="codex-interposer", daemon=True)
    thread.start()

    if not ready.wait(timeout=ready_timeout):
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=ready_timeout)
        raise RuntimeError("Codex interposer did not become ready in time.")
    if error := holder.get("error"):
        raise RuntimeError("Codex interposer failed to start.") from error

    def stop() -> None:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=ready_timeout)

    return holder["port"], stop
