"""Component coverage; the loopback peer substitutes only the native app-server."""

import json
import threading

import pytest
from websockets.sync.server import serve

from ucode.smart_routing import codex_interposer, codex_resume


class NativePeer:
    def __init__(self):
        self.threads = {}
        self.calls = []

    def add(self, child, parent, model="child-model", effort="high"):
        self.threads[child] = {
            "id": child,
            "parentThreadId": parent,
            "model": model,
            "reasoningEffort": effort,
            "canAcceptDirectInput": True,
            "status": {"type": "idle"},
        }

    def handle(self, connection):
        for raw in connection:
            call = json.loads(raw)
            if "id" not in call:
                continue
            self.calls.append(call)
            method, params = call["method"], call["params"]
            result = {}
            event = None
            if method == "thread/read":
                result = {"thread": self.threads[params["threadId"]]}
            elif method == "thread/resume":
                result = {
                    "collaborationMode": {
                        "mode": "plan",
                        "settings": {
                            "model": "parent-model",
                            "reasoning_effort": "low",
                            "developer_instructions": "Retain this instruction.",
                        },
                    }
                }
            elif method == "thread/settings/update":
                thread = self.threads[params["threadId"]]
                effort = params.get("effort")
                if "collaborationMode" in params:
                    effort = params["collaborationMode"]["settings"]["reasoning_effort"]
                thread.update(model=params["model"], reasoningEffort=effort)
                event = {
                    "method": "thread/settings/updated",
                    "params": {
                        "threadId": params["threadId"],
                        "threadSettings": {"model": params["model"], "effort": effort},
                    },
                }
            connection.send(json.dumps({"id": call["id"], "result": result}))
            if event:
                connection.send(json.dumps(event))


@pytest.fixture
def native_peer():
    peer = NativePeer()
    with serve(peer.handle, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        peer.url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        try:
            yield peer
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def spawn(
    routes, peer, child="child", parent="parent", *, routed=True, call_id="spawn-1", root=None
):
    payload = {
        "session_id": root or parent,
        "tool_use_id": call_id,
        "tool_response": json.dumps({"agent_id": child}),
    }
    if root is not None:
        payload["agent_id"] = parent
    if routed:
        routes.remember_spawn(payload, peer.threads[child]["model"])
    with codex_resume.AppServer(peer.url) as client:
        routes.bind_spawn(payload, client)


def settings_calls(peer):
    return [c["params"] for c in peer.calls if c["method"] == "thread/settings/update"]


@pytest.mark.parametrize("effort", ["high", None])
def test_resume_restores_saved_model_and_effort_across_guard_restart(tmp_path, native_peer, effort):
    peer = native_peer
    peer.add("child", "parent", effort=effort)
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        codex_resume.guard_tool(
            {
                "tool_name": "multi_agent_v1resume_agent",
                "tool_input": {
                    "id": "child",
                },
            },
            routes,
            client,
        )
    # The external native v1 runtime incorrectly inherits these parent defaults.
    peer.threads["child"].update(model="parent-model", reasoningEffort="low")
    routes = codex_resume.RoutedChildren(routes.path)
    with codex_resume.AppServer(peer.url) as client:
        codex_resume.guard_tool(
            {
                "tool_name": "multi_agent_v1send_input",
                "tool_input": {
                    "target": "child",
                    "message": "Continue.",
                },
            },
            routes,
            client,
        )
    assert peer.threads["child"]["model"] == "child-model"
    assert peer.threads["child"]["reasoningEffort"] == effort
    (update,) = settings_calls(peer)
    if effort is None:
        assert update == {
            "threadId": "child",
            "model": "child-model",
            "collaborationMode": {
                "mode": "plan",
                "settings": {
                    "model": "child-model",
                    "reasoning_effort": None,
                    "developer_instructions": "Retain this instruction.",
                },
            },
        }
    else:
        assert update == {"threadId": "child", "model": "child-model", "effort": effort}


def test_live_followups_and_unrouted_children_keep_their_current_settings(tmp_path, native_peer):
    peer = native_peer
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    for child, routed in (("routed", True), ("unrouted", False)):
        peer.add(child, "parent")
        spawn(routes, peer, child, routed=routed, call_id=child)
        peer.threads[child].update(model="user-selection", reasoningEffort="medium")
    with codex_resume.AppServer(peer.url) as client:
        routes.restore_before_input("routed", client)
        routes.prepare_resume("unrouted", client)
        routes.restore_before_input("unrouted", client)
        routes.restore_before_input("parent", client)
    assert settings_calls(peer) == []
    assert {t["model"] for t in peer.threads.values()} == {"user-selection"}


def test_resume_preserves_deliberate_model_change_before_close(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    peer.threads["child"].update(model="user-selection", reasoningEffort="medium")
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
        peer.threads["child"].update(model="parent-model", reasoningEffort="low")
        # A repeated resume before input must not replace the saved settings.
        routes.prepare_resume("child", client)
        routes.restore_before_input("child", client)
    assert settings_calls(peer) == [
        {
            "threadId": "child",
            "model": "user-selection",
            "effort": "medium",
        }
    ]


def test_resume_protects_routed_descendants_through_unrouted_parent(tmp_path, native_peer):
    peer = native_peer
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    peer.add("middle", "parent")
    peer.add("leaf", "middle", model="leaf-model", effort="max")
    peer.add("sibling", "parent", model="sibling-model")
    spawn(routes, peer, "middle", routed=False)
    spawn(routes, peer, "leaf", "middle", root="parent")
    spawn(routes, peer, "sibling", call_id="spawn-2")
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("middle", client)
        for thread in peer.threads.values():
            thread.update(model="parent-model", reasoningEffort="low")
        routes.restore_before_input("leaf", client)
        routes.restore_before_input("sibling", client)
    assert settings_calls(peer) == [{"threadId": "leaf", "model": "leaf-model", "effort": "max"}]


def test_nested_spawn_with_same_call_id_is_bound_to_its_actual_parent(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    peer.add("leaf", "child", model="leaf-model", effort="medium")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    root = {"session_id": "parent", "tool_use_id": "spawn", "tool_response": {"agent_id": "child"}}
    nested = {
        "session_id": "parent",
        "agent_id": "child",
        "tool_use_id": "spawn",
        "tool_response": {"agent_id": "leaf"},
    }
    routes.remember_spawn(root, "child-model")
    routes.remember_spawn(nested, "leaf-model")
    with codex_resume.AppServer(peer.url) as client:
        routes.bind_spawn(nested, client)
        routes.bind_spawn(root, client)
        routes.prepare_resume("child", client)
        for thread in peer.threads.values():
            thread.update(model="parent-model", reasoningEffort="low")
        routes.restore_before_input("child", client)
        routes.restore_before_input("leaf", client)
    assert settings_calls(peer) == [
        {"threadId": "child", "model": "child-model", "effort": "high"},
        {"threadId": "leaf", "model": "leaf-model", "effort": "medium"},
    ]


def test_guard_does_not_resume_an_unloaded_child(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
        peer.threads["child"]["status"] = {"type": "notLoaded"}
        with pytest.raises(RuntimeError, match="Resume the child"):
            routes.restore_before_input("child", client)
    assert "thread/resume" not in [c["method"] for c in peer.calls]


def test_native_v2_is_not_registered_for_v1_repairs(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    peer.threads["child"]["canAcceptDirectInput"] = False
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
        routes.restore_before_input("child", client)
    assert settings_calls(peer) == []


def test_child_identity_mismatch_does_not_bind_a_route(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "unrelated-parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    with pytest.raises(ValueError, match="do not match"):
        spawn(routes, peer)


def test_explicit_override_after_resume_releases_the_guard(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
        peer.threads["child"].update(model="user-selection")
        routes.allow_override("child")
        routes.restore_before_input("child", client)
    assert settings_calls(peer) == []
    assert peer.threads["child"]["model"] == "user-selection"


@pytest.mark.parametrize("method", ["turn/start", "thread/settings/update", "thread/resume"])
@pytest.mark.parametrize("accepted", [True, False])
def test_only_successful_native_overrides_release_the_guard(
    tmp_path, native_peer, method, accepted
):
    peer = native_peer
    peer.add("child", "parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
    peer.threads["child"].update(model="parent-model", reasoningEffort="low")
    acknowledged = []

    def override(target):
        acknowledged.append(target)
        routes.allow_override(target)

    session = codex_interposer._Session(None, log=lambda _message: None, on_model_override=override)
    request = json.dumps(
        {
            "id": 7,
            "method": method,
            "params": {
                "threadId": "child",
                "model": "user-selection",
                "effort": "medium",
            },
        }
    )
    assert session.on_tui_frame(request).frame == request
    assert acknowledged == []
    session.on_engine_frame(json.dumps({"id": "unrelated", "result": {}}))
    assert acknowledged == []
    if accepted:
        peer.threads["child"].update(model="user-selection", reasoningEffort="medium")
    response = (
        {"result": {}} if accepted else {"error": {"code": -32602, "message": "Invalid model"}}
    )
    session.on_engine_frame(json.dumps({"id": 7, **response}))
    assert acknowledged == (["child"] if accepted else [])
    with codex_resume.AppServer(peer.url) as client:
        routes.restore_before_input("child", client)
    assert peer.threads["child"]["model"] == ("user-selection" if accepted else "child-model")
    assert peer.threads["child"]["reasoningEffort"] == ("medium" if accepted else "high")


def test_overlapping_resume_cannot_replace_a_pending_snapshot(tmp_path, native_peer):
    peer = native_peer
    peer.add("child", "parent")
    routes = codex_resume.RoutedChildren(tmp_path / "routes.sqlite3")
    spawn(routes, peer)
    with codex_resume.AppServer(peer.url) as client:
        routes.prepare_resume("child", client)
        peer.threads["child"].update(model="parent-model", reasoningEffort="low")
        # A second guard may have read pending=0 before the first guard committed.
        routes.capture("child", "parent", client.read("child"), pending=True)
        routes.restore_before_input("child", client)
    assert settings_calls(peer) == [{"threadId": "child", "model": "child-model", "effort": "high"}]
