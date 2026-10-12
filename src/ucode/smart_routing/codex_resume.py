"""Preserve routed Codex v1 child settings across native close/resume calls."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from ucode import config_io
from ucode.smart_routing.session_env import session_env_path

RPC_TIMEOUT_SECONDS = 15
GUARD_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    sqlite3.Error,
    WebSocketException,
)


def _caller(payload: dict) -> str | None:
    # Codex keeps session_id at the root for hooks run by descendants.
    value = payload.get("agent_id") or payload.get("session_id")
    return value if isinstance(value, str) else None


class AppServer:
    """A short-lived native client; never starts or resumes an unloaded child."""

    def __init__(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1":
            raise ValueError("Codex resume protection requires a loopback app-server")
        self.url = url
        if parsed.port is None:
            raise ValueError("Codex app-server URL has no port")
        self.port = parsed.port
        self.next_id = 0
        self.events: list[dict] = []
        self.connection = None
        self.socket = None

    def __enter__(self):
        return self

    def _open(self):
        # Passing a socket works with websockets 13+ and prevents environment
        # proxy settings from forwarding this local control connection.
        transport = socket.create_connection(("127.0.0.1", self.port), RPC_TIMEOUT_SECONDS)
        try:
            self.connection = connect(self.url, sock=transport, open_timeout=RPC_TIMEOUT_SECONDS)
            self.socket = self.connection.__enter__()
        except Exception:
            transport.close()
            raise
        self.deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "ug-resume-guard", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.socket.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.connection.__exit__(None, None, None)
            self.connection = None
            self.socket = None
            raise

    def __exit__(self, *_exc):
        if self.connection is not None:
            self.connection.__exit__(*_exc)

    def receive(self) -> dict:
        assert self.socket is not None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex settings restoration timed out")
        message = json.loads(self.socket.recv(timeout=remaining))
        if not isinstance(message, dict):
            raise ValueError("Invalid Codex app-server response")
        return message

    def call(self, method: str, params: dict) -> dict:
        if self.socket is None:
            self._open()
        assert self.socket is not None
        self.next_id += 1
        self.socket.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = self.receive()
            if message.get("id") == self.next_id:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise RuntimeError(f"Codex rejected {method}")
                return message["result"]
            self.events.append(message)

    def read(self, thread_id: str) -> dict:
        thread = self.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get(
            "thread"
        )
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise ValueError("Invalid Codex thread settings")
        return thread

    def restore(self, thread_id: str, model: str, effort: str | None) -> None:
        thread = self.read(thread_id)
        status = thread.get("status")
        if not isinstance(status, dict):
            raise ValueError("Invalid Codex thread status")
        if status.get("type") == "notLoaded":
            raise RuntimeError("Resume the child before sending follow-up input")
        if thread.get("canAcceptDirectInput") is not True:
            raise RuntimeError("Child does not accept native v1 settings updates")
        if "reasoningEffort" in thread and (thread.get("model"), thread["reasoningEffort"]) == (
            model,
            effort,
        ):
            return
        # Attach only after proving the child is loaded. Native resume_agent owns
        # subtree restoration and graph bookkeeping; the guard must not replace it.
        current = self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        update: dict = {"threadId": thread_id, "model": model}
        if effort is not None:
            update["effort"] = effort
        else:
            # The public effort:null field means "unchanged". A collaboration
            # settings update can clear it while retaining the current mode.
            mode = copy.deepcopy(current["collaborationMode"])
            if not isinstance(mode, dict) or not isinstance(mode.get("settings"), dict):
                raise ValueError("Invalid Codex collaboration mode")
            mode["settings"].update(model=model, reasoning_effort=None)
            update["collaborationMode"] = mode
        self.events.clear()
        self.call("thread/settings/update", update)
        # The RPC acknowledgement only means queued, not applied. Do not release
        # send_input until the native thread confirms both settings.
        while True:
            event = self.events.pop(0) if self.events else self.receive()
            params = event.get("params")
            if not isinstance(params, dict):
                continue
            settings = params.get("threadSettings")
            if (
                event.get("method") == "thread/settings/updated"
                and params.get("threadId") == thread_id
                and isinstance(settings, dict)
                and "effort" in settings
                and (settings.get("model"), settings.get("effort")) == (model, effort)
            ):
                return


class RoutedChildren:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute(
                "CREATE TABLE IF NOT EXISTS spawns "
                "(parent TEXT, call_id TEXT, model TEXT NOT NULL, PRIMARY KEY(parent, call_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS children "
                "(id TEXT PRIMARY KEY, parent TEXT NOT NULL, model TEXT, effort TEXT, "
                "ready INTEGER NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0)"
            )
            yield db

    def remember_spawn(self, payload: dict, model: str) -> None:
        parent, call_id = _caller(payload), payload.get("tool_use_id")
        if not isinstance(parent, str) or not isinstance(call_id, str):
            raise ValueError("Codex omitted the routed spawn identity")
        with self.database() as db:
            db.execute("INSERT OR REPLACE INTO spawns VALUES (?, ?, ?)", (parent, call_id, model))

    def bind_spawn(self, payload: dict, client: AppServer) -> None:
        response = payload.get("tool_response")
        if isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict) or not isinstance(response.get("agent_id"), str):
            return
        child = response["agent_id"]
        parent, call_id = _caller(payload), payload.get("tool_use_id")
        if not isinstance(parent, str) or not isinstance(call_id, str):
            return
        with self.database() as db:
            spawn = db.execute(
                "SELECT model FROM spawns WHERE parent=? AND call_id=?", (parent, call_id)
            ).fetchone()
            # Retain unrouted edges too: a routed descendant may have an unrouted parent.
            db.execute(
                "INSERT OR IGNORE INTO children (id, parent, model) VALUES (?, ?, ?)",
                (child, parent, spawn["model"] if spawn else None),
            )
        if spawn is None:
            return
        thread = client.read(child)
        if thread.get("canAcceptDirectInput") is False:
            # Native v2 has its own persisted-model reload path.
            with self.database() as db:
                db.execute("UPDATE children SET model=NULL WHERE id=?", (child,))
                db.execute("DELETE FROM spawns WHERE parent=? AND call_id=?", (parent, call_id))
            return
        self.capture(child, parent, thread, expected_model=spawn["model"])
        with self.database() as db:
            db.execute("DELETE FROM spawns WHERE parent=? AND call_id=?", (parent, call_id))

    def capture(
        self,
        child: str,
        parent: str,
        thread: dict,
        *,
        expected_model: str | None = None,
        pending: bool = False,
    ) -> None:
        model, effort = thread.get("model"), thread.get("reasoningEffort")
        if (
            thread.get("id") != child
            or thread.get("parentThreadId") != parent
            or not isinstance(model, str)
            or "reasoningEffort" not in thread
            or (effort is not None and not isinstance(effort, str))
            or (expected_model is not None and model != expected_model)
        ):
            raise ValueError("Codex child settings do not match the routed spawn")
        with self.database() as db:
            db.execute(
                "UPDATE children SET model=?, effort=?, ready=1, pending=? WHERE id=? AND pending=0",
                (model, effort, pending, child),
            )

    def prepare_resume(self, target: str, client: AppServer) -> None:
        with self.database() as db:
            children = db.execute(
                "WITH RECURSIVE tree(id) AS (SELECT ? UNION SELECT c.id FROM children c "
                "JOIN tree t ON c.parent=t.id) SELECT c.* FROM children c JOIN tree t ON c.id=t.id "
                "WHERE c.model IS NOT NULL",
                (target,),
            ).fetchall()
        for child in children:
            if child["pending"]:
                continue
            # Capture deliberate changes made before closing, but never recapture
            # the inherited defaults from an earlier, still-pending native resume.
            self.capture(
                child["id"],
                child["parent"],
                client.read(child["id"]),
                expected_model=child["model"] if not child["ready"] else None,
                pending=True,
            )

    def restore_before_input(self, target: str, client: AppServer) -> None:
        with self.database() as db:
            child = db.execute("SELECT * FROM children WHERE id=?", (target,)).fetchone()
        if child is None or child["model"] is None or not child["pending"]:
            return
        if not child["ready"]:
            raise RuntimeError("The routed child's original settings were not captured")
        client.restore(target, child["model"], child["effort"])
        self.allow_override(target)

    def allow_override(self, target: str) -> None:
        with self.database() as db:
            db.execute("UPDATE children SET pending=0 WHERE id=?", (target,))


def for_workspace(workspace: str) -> RoutedChildren:
    key = hashlib.sha256(workspace.rstrip("/").encode()).hexdigest()
    return RoutedChildren(config_io.APP_DIR / "codex-routed-children" / f"{key}.sqlite3")


def configure_session(path: Path, url: str) -> None:
    # Keep the port out of command text: changing it would invalidate native
    # hook trust on every launch.
    config_io.atomic_write_json(path.parent / "codex-app-server.json", {"url": url})


def app_server_url() -> str | None:
    try:
        path = session_env_path().parent / "codex-app-server.json"
    except RuntimeError:
        return None
    if not path.exists():
        return None
    metadata = json.loads(path.read_text(encoding="utf-8"))
    value = metadata.get("url") if isinstance(metadata, dict) else None
    if not isinstance(value, str):
        raise ValueError("Invalid Codex app-server session metadata")
    return value


def guard_tool(payload: dict, routes: RoutedChildren, client: AppServer) -> None:
    tool = payload.get("tool_name", "")
    args = payload.get("tool_input")
    if not isinstance(args, dict) or not isinstance(tool, str):
        return
    resume = tool.endswith("resume_agent")
    target = args.get("id") if resume else args.get("target", args.get("id"))
    if not isinstance(target, str):
        return
    if resume:
        routes.prepare_resume(target, client)
    elif tool.endswith("send_input"):
        routes.restore_before_input(target, client)
