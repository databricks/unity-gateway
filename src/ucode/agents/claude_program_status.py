"""Publish delegated Claude work through OSC 7501 without reading the screen.

Claude's extension can read its agent registry but cannot write terminal bytes.
It supplies a small snapshot file instead. One launcher-owned worker turns those
snapshots into independent child tasks, leaving Claude's own reports untouched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

SNAPSHOT_ENV = "UG_CLAUDE_PROGRAM_STATUS_FILE"


@dataclass(frozen=True)
class Publication:
    """A launch's snapshot destination and its post-spawn activation callback."""

    path: Path | None
    start: Callable[[], None]


# Waiting means Blocked. A separate, confirmed input request can supply a reason;
# the registry alone cannot distinguish approval from waiting for another task.
_OSC_STATES = {
    "pending": "working",
    "running": "working",
    "waiting": "blocked",
    "idle": "idle",
    "completed": "done",
    "failed": "error",
    "killed": "clear",
}


def _report(
    state: str,
    task_id: str,
    title: str | None = None,
    kind: Literal["question"] | None = None,
) -> bytes:
    fields = f"state={state}:id={task_id}:app=unity-gateway"
    if state == "blocked" and kind is not None:
        fields += f":kind={kind}"
    if title is not None:
        # OSC titles are base64 text with a 192-byte decoded limit. Do not let
        # agent-written names inject terminal controls or invalidate the report.
        cleaned = "".join(char for char in title if char.isprintable()) or "Subagent"
        encoded = cleaned.encode("utf-8", errors="replace")[:192].decode("utf-8", errors="ignore")
        fields += ":title=" + base64.b64encode(encoded.encode("utf-8")).decode("ascii")
    return f"\x1b]7501;{fields}\x07".encode()


@dataclass(frozen=True)
class SubagentTask:
    """One agent's identity, status, title, and any confirmed input reason."""

    agent_id: str
    status: str
    title: str
    kind: Literal["question"] | None = None

    @classmethod
    def parse(cls, value: object) -> SubagentTask | None:
        if not isinstance(value, dict):
            return None
        agent_id, state, title = (value.get(key) for key in ("id", "status", "title"))
        if not isinstance(agent_id, str) or not agent_id or not isinstance(title, str):
            return None
        if not isinstance(state, str) or state not in _OSC_STATES:
            return None
        kind = value.get("kind")
        if kind is not None and (kind != "question" or state != "waiting"):
            return None
        try:
            agent_id.encode("utf-8")
        except UnicodeError:
            return None
        return cls(agent_id, state, title, kind)

    def task_id(self, namespace: str) -> str:
        # Provider ids can contain slashes or exceed OSC's segment limits. A
        # stable digest keeps them distinct, valid, and scoped to this launch.
        return f"{namespace}/{hashlib.sha256(self.agent_id.encode()).hexdigest()[:32]}"

    def report(self, namespace: str) -> bytes:
        state = _OSC_STATES[self.status]
        return _report(
            state, self.task_id(namespace), None if state == "clear" else self.title, self.kind
        )


@dataclass(frozen=True)
class SubagentSnapshot:
    """A complete immutable agent list in one conversation, or its explicit end."""

    session_id: str
    agents: tuple[SubagentTask, ...] | None

    @classmethod
    def read(cls, path: Path) -> SubagentSnapshot | None:
        """An incomplete or unreadable write supplies no new information.

        Reject the entire snapshot if any row is invalid. Dropping an unreadable
        row would falsely remove an agent's last known status.
        """
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
            return None
        session_id = value["sessionId"]
        if not session_id or "agents" not in value:
            return None
        if value["agents"] is None:
            return cls(session_id, None)
        if not isinstance(value["agents"], list):
            return None
        agents: list[SubagentTask] = []
        identities: set[str] = set()
        for row in value["agents"]:
            agent = SubagentTask.parse(row)
            if agent is None or agent.agent_id in identities:
                return None
            agents.append(agent)
            identities.add(agent.agent_id)
        return cls(session_id, tuple(agents))

    def reports_after(self, previous: SubagentSnapshot | None, namespace: str) -> tuple[bytes, ...]:
        """Report each changed agent independently, without aggregating siblings.

        An agent finishing cannot hide another still working. Cancellation and
        disappearance clear that agent instead of inventing success. Replacing a
        conversation clears only this launch's namespace before its new reports.
        """
        replaced = previous is not None and previous.session_id != self.session_id
        reports = [_report("clear", namespace)] if replaced else []
        if self.agents is None:
            if not replaced and previous is not None:
                reports.append(_report("clear", namespace))
            return tuple(reports)
        before = (
            {}
            if previous is None or replaced
            else {agent.agent_id: agent for agent in previous.agents or ()}
        )
        current = {agent.agent_id: agent for agent in self.agents}
        for agent_id in sorted(before.keys() - current.keys()):
            reports.append(_report("clear", before[agent_id].task_id(namespace)))
        for agent in self.agents:
            report = agent.report(namespace)
            old = before.get(agent.agent_id)
            if old is None or old.report(namespace) != report:
                reports.append(report)
        return tuple(reports)


def _write_report(
    output_fd: int, report: bytes, *, cancelled: threading.Event | None = None
) -> None:
    """Finish a record through partial writes and temporary terminal backpressure.

    Claude may make the shared terminal descriptor nonblocking. A full output
    buffer must not permanently disable reporting. Waiting happens only on the
    publisher worker and ends when shutdown cancels the pending report.
    """
    remaining = memoryview(report)
    cancelled = cancelled or threading.Event()
    while remaining:
        if cancelled.is_set():
            return
        try:
            written = os.write(output_fd, remaining)
        except BlockingIOError:
            cancelled.wait(0.01)
            continue
        if written == 0:
            raise OSError("Terminal status write made no progress")
        remaining = remaining[written:]


@contextmanager
def publish(path: Path, *, enabled: bool, output_fd: int | None = None) -> Iterator[Publication]:
    """Own the terminal publisher for exactly one interactive Claude launch.

    File reads happen on a worker, outside inference and terminal input handling.
    That worker alone writes reports and clears its tasks before closing its copy
    of the output descriptor. Headless output and explicit title/status opt-outs
    never start a publisher.

    Activation is explicit: the launcher calls start only after spawning Claude,
    so the first-prompt PTY path does not inherit this worker across its fork.
    """
    if not enabled or os.environ.get("CLAUDE_CODE_DISABLE_TERMINAL_TITLE"):
        yield Publication(None, lambda: None)
        return
    try:
        descriptor = sys.stdout.fileno() if output_fd is None else output_fd
        descriptor = os.dup(descriptor) if os.isatty(descriptor) else None
    except (OSError, ValueError):
        descriptor = None
    if descriptor is None:
        yield Publication(None, lambda: None)
        return
    output_fd = descriptor

    stopped = threading.Event()
    task_namespace = f"unity-gateway/{uuid4().hex}"

    def run() -> None:
        previous: SubagentSnapshot | None = None
        revision: tuple[int, int, int] | None = None
        try:
            while not stopped.is_set():
                try:
                    info = path.stat()
                    observed = (info.st_ino, info.st_mtime_ns, info.st_size)
                except OSError:
                    observed = None
                # Finished agents remain in the inventory. Decode only changed
                # files, rather than repeatedly parsing a growing agent list.
                if observed is not None and observed != revision:
                    current = SubagentSnapshot.read(path)
                    if stopped.is_set():
                        break
                    if current is not None:
                        for report in current.reports_after(previous, task_namespace):
                            if stopped.is_set():
                                break
                            _write_report(output_fd, report, cancelled=stopped)
                        previous, revision = current, observed
                stopped.wait(0.1)
        except OSError:
            # A closed terminal must not interrupt or restart the agent.
            pass
        finally:
            # Clear even after a failed first write: part of a multi-agent
            # update may already be visible although the snapshot was not saved.
            with suppress(OSError):
                _write_report(output_fd, _report("clear", task_namespace))
            os.close(output_fd)

    worker = threading.Thread(target=run, name="claude-program-status", daemon=True)
    started = False

    def start() -> None:
        nonlocal started
        if not started:
            try:
                worker.start()
                started = True
            except RuntimeError:
                # Optional status reporting cannot prevent the agent launching.
                pass

    try:
        yield Publication(path, start)
    finally:
        stopped.set()
        # A stalled filesystem must not hold the launcher open. The worker
        # checks stopped after its read, so it cannot publish late Working.
        if started:
            worker.join(timeout=1)
        else:
            os.close(output_fd)
