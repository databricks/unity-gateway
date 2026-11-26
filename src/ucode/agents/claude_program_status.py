"""Publish delegated Claude work through OSC 7501 without reading the screen.

Claude's extension can read its agent registry but cannot write terminal bytes.
It supplies a small snapshot file instead. One launcher-owned worker turns those
snapshots into an independent task, leaving Claude's own reports untouched.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

SNAPSHOT_ENV = "UG_CLAUDE_PROGRAM_STATUS_FILE"


@dataclass(frozen=True)
class Publication:
    """A launch's snapshot destination and its post-spawn activation callback."""

    path: Path | None
    start: Callable[[], None]


@dataclass(frozen=True)
class SubagentSnapshot:
    """Outstanding delegated work in one conversation, or its explicit end."""

    session_id: str
    active_tasks: int | None

    @classmethod
    def read(cls, path: Path) -> SubagentSnapshot | None:
        """An incomplete or unreadable write supplies no new information."""
        try:
            with path.open(encoding="utf-8") as stream:
                text = stream.read(4097)
            if len(text) > 4096:
                return None
            value = json.loads(text)
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
            return None
        session_id = value["sessionId"]
        count = value.get("activeTasks")
        if not session_id or "activeTasks" not in value:
            return None
        if count is not None and (type(count) is not int or count < 0):
            return None
        return cls(session_id, count)

    def reports_after(self, previous: SubagentSnapshot | None, task_id: str) -> bytes:
        """Only an observed transition to no outstanding work reports completion.

        A conversation replacement or exit clears our task instead. Neither is
        evidence that its work finished. Task identity belongs to this launch,
        so a clear cannot remove Claude's root or another program's children.
        """

        def report(state: str) -> bytes:
            return f"\x1b]7501;state={state}:id={task_id}:app=unity-gateway\x07".encode()

        replaced = previous is not None and previous.session_id != self.session_id
        prefix = report("clear") if replaced else b""
        if self.active_tasks is None:
            return prefix or (report("clear") if previous is not None else b"")
        was_working = previous is not None and previous.active_tasks not in (None, 0)
        if self.active_tasks > 0:
            return prefix + (report("working") if replaced or not was_working else b"")
        return prefix + (report("done") if was_working and not replaced else b"")


@contextmanager
def publish(path: Path, *, enabled: bool, output_fd: int | None = None) -> Iterator[Publication]:
    """Own the terminal publisher for exactly one interactive Claude launch.

    File reads happen on a worker, outside inference and terminal input handling.
    That worker alone writes reports and clears its task before closing its copy
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
    task_id = f"unity-gateway/{uuid4().hex}"

    def run() -> None:
        previous: SubagentSnapshot | None = None
        try:
            while not stopped.is_set():
                current = SubagentSnapshot.read(path)
                if stopped.is_set():
                    break
                if current is not None and current != previous:
                    if reports := current.reports_after(previous, task_id):
                        os.write(output_fd, reports)
                    previous = current
                stopped.wait(0.1)
        except OSError:
            # A closed terminal must not interrupt or restart the agent.
            pass
        finally:
            if previous is not None:
                with suppress(OSError):
                    os.write(output_fd, f"\x1b]7501;state=clear:id={task_id}\x07".encode())
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
