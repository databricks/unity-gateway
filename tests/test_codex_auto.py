"""Codex's native Auto picker keeps a real main model behind the menu row."""

import asyncio
import json
from uuid import uuid4

from ucode.smart_routing.codex_auto import AUTO_MODEL, CodexAutoPicker
from ucode.smart_routing.codex_feedback import CodexFeedbackInput, CodexSmartRouter, ThreadOwnership
from ucode.smart_routing.session_env import start_session


def model(name):
    return {
        "id": name,
        "model": name,
        "displayName": name,
        "description": name,
        "hidden": False,
        "isDefault": False,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": "Medium"}],
    }


def test_auto_picker_main_model_manual_switch_and_cancel(monkeypatch):
    asyncio.run(_auto_picker_main_model_manual_switch_and_cancel(monkeypatch))


async def _auto_picker_main_model_manual_switch_and_cancel(monkeypatch):
    monkeypatch.setenv("ISAAC_SMART_ROUTER_FEEDBACK", "0")
    controls = start_session({})
    router = CodexSmartRouter(controls)
    router.feedback_required = False
    thread_id = str(uuid4())
    router.threads[thread_id] = ThreadOwnership.ROOT
    requests = []
    notices = []
    answers = []
    picker = CodexAutoPicker(
        router,
        CodexFeedbackInput(lambda frame: asyncio.sleep(0)),
        lambda frame: requests.append(json.loads(frame)) or asyncio.sleep(0),
        lambda frame: notices.append(json.loads(frame)) or asyncio.sleep(0),
    )
    picker.thread_id = thread_id
    picker.settings = {
        "model": "gpt-one",
        "effort": "medium",
        "approvalPolicy": "never",
        "approvalsReviewer": None,
        "collaborationMode": None,
        "cwd": "/tmp",
        "modelProvider": "Databricks",
        "sandboxPolicy": {},
    }
    result = {"id": 1, "result": {"data": [model("gpt-one"), model("gpt-two")]}}
    picker.requests[1] = "model/list"
    await picker.observe(result)
    assert result["result"]["data"][0]["model"] == AUTO_MODEL

    async def ask(question):
        return answers.pop(0)

    picker.ask = ask

    async def reply():
        while True:
            await asyncio.sleep(0)
            for request in list(requests):
                if request["id"] not in seen:
                    seen.add(request["id"])
                    await picker.observe({"id": request["id"], "result": {}})

    seen = set()
    responder = asyncio.create_task(reply())
    try:
        answers.append(None)
        await picker.choose(AUTO_MODEL)
        assert picker.main_model == "gpt-one"
        assert not requests

        answers.append(["gpt-two"])
        await picker.choose(AUTO_MODEL)
        assert picker.enabled
        assert requests[-2]["params"]["model"] == "gpt-two"
        assert requests[-1]["params"]["edits"][0]["value"] == "gpt-two"
        assert notices[-1]["params"]["threadSettings"]["model"] == AUTO_MODEL

        picker.settings["model"] = "gpt-two"
        await picker.choose("gpt-one")
        assert not picker.enabled
        assert requests[-2]["params"]["model"] == "gpt-one"
        assert notices[-1]["params"]["threadSettings"]["model"] == "gpt-one"
    finally:
        responder.cancel()
        await asyncio.gather(responder, return_exceptions=True)


def test_native_auto_selection_never_reaches_engine():
    asyncio.run(_native_auto_selection_never_reaches_engine())


async def _native_auto_selection_never_reaches_engine():
    controls = start_session({})
    router = CodexSmartRouter(controls)
    thread_id = str(uuid4())
    router.threads[thread_id] = ThreadOwnership.ROOT
    picker = CodexAutoPicker(
        router,
        CodexFeedbackInput(lambda frame: asyncio.sleep(0)),
        lambda frame: asyncio.sleep(0),
        lambda frame: asyncio.sleep(0),
    )
    picker.thread_id = thread_id
    picker.settings = {
        "model": "gpt-one",
        "effort": "medium",
        "approvalPolicy": "never",
        "approvalsReviewer": None,
        "collaborationMode": None,
        "cwd": "/tmp",
        "modelProvider": "Databricks",
        "sandboxPolicy": {},
    }
    picker.models = {"gpt-one": model("gpt-one")}
    update = {
        "id": 1,
        "method": "thread/settings/update",
        "params": {"threadId": thread_id, "model": AUTO_MODEL, "effort": "high"},
    }
    assert not await picker.before_tui(update)
    assert update["params"] == {"threadId": thread_id, "model": "gpt-one"}
    assert picker.selections[1] == AUTO_MODEL
    save = {
        "id": 2,
        "method": "config/batchWrite",
        "params": {
            "edits": [
                {"keyPath": "model", "value": AUTO_MODEL},
                {"keyPath": "model_reasoning_effort", "value": "high"},
            ]
        },
    }
    assert not await picker.before_tui(save)
    assert [edit["value"] for edit in save["params"]["edits"]] == ["gpt-one", "medium"]


def test_observe_ignores_request_id_collisions_and_bad_catalog_rows(monkeypatch):
    asyncio.run(_observe_ignores_request_id_collisions_and_bad_catalog_rows(monkeypatch))


async def _observe_ignores_request_id_collisions_and_bad_catalog_rows(monkeypatch):
    from ucode.smart_routing import codex_auto

    controls = start_session({})
    router = CodexSmartRouter(controls)
    sent = []
    picker = CodexAutoPicker(
        router,
        CodexFeedbackInput(lambda frame: asyncio.sleep(0)),
        lambda frame: asyncio.sleep(0),
        lambda frame: sent.append(json.loads(frame)) or asyncio.sleep(0),
    )
    picker.requests[7] = "model/list"
    picker.selections[7] = AUTO_MODEL
    assert await picker.observe({"id": 7, "method": "warning", "params": {}}) == (False, None)
    assert picker.requests[7] == "model/list"
    assert picker.selections[7] == AUTO_MODEL
    result = {"id": 7, "result": {"data": [None, {"model": "missing-effort"}, model("gpt-one")]}}
    assert await picker.observe(result) == (False, AUTO_MODEL)
    assert list(picker.models) == ["gpt-one"]
    assert result["result"]["data"][0]["model"] == AUTO_MODEL

    thread_id = str(uuid4())
    picker.thread_id = thread_id
    await picker.observe(
        {"method": "turn/started", "params": {"threadId": thread_id, "turn": None}}
    )
    assert picker.turn_id == ""
    monkeypatch.setattr(
        codex_auto, "atomic_write_json", lambda *_a: (_ for _ in ()).throw(OSError("read only"))
    )
    await picker.observe(
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": thread_id,
                "threadSettings": {"model": "gpt-one", "effort": "medium"},
            },
        }
    )
    assert picker.saved_model is None


def test_failed_controls_read_and_rollback_do_not_lock_picker():
    asyncio.run(_failed_controls_read_and_rollback_do_not_lock_picker())


async def _failed_controls_read_and_rollback_do_not_lock_picker():
    controls = start_session({})
    router = CodexSmartRouter(controls)
    thread_id = str(uuid4())
    router.threads[thread_id] = ThreadOwnership.ROOT
    notices = []
    picker = CodexAutoPicker(
        router,
        CodexFeedbackInput(lambda frame: asyncio.sleep(0)),
        lambda frame: asyncio.sleep(0),
        lambda frame: notices.append(json.loads(frame)) or asyncio.sleep(0),
    )
    picker.thread_id = thread_id
    picker.settings = {"model": "gpt-one", "effort": "medium"}
    picker.models = {"gpt-one": model("gpt-one")}
    picker.busy = True
    controls.unlink()
    await picker.choose(AUTO_MODEL)
    assert not picker.busy
    assert notices[-1]["method"] == "warning"

    controls.write_text("{}")
    notices.clear()
    picker.busy = True
    picker.ask = lambda question: asyncio.sleep(0, result=["gpt-one"])

    async def fail_request(_method, _params):
        raise TimeoutError("injected timeout")

    picker.request = fail_request
    await picker.choose(AUTO_MODEL)
    assert not picker.busy
    warning = next(item for item in notices if item["method"] == "warning")
    assert "injected timeout" in warning["params"]["message"]
    assert "Could not restore the previous selection" in warning["params"]["message"]
