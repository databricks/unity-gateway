"""PTY wrapper that routes Claude Code's first prompt with ``/model <name>``.

Claude Code has no runtime control protocol, so a per-launch UserPromptSubmit
hook captures and blocks the first prompt. While the TUI is idle, this wrapper
types a direct model command, restores the user's persisted default model, and
replays the prompt. Claude Code keeps a blocked prompt in its input box, so every
write into the box first clears it. Terminal traffic is otherwise forwarded unchanged.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import termios
import threading
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_MODEL_NAME_LEN = 200
CONFIRM_TIMEOUT_S = 3.0
SWITCH_TIMEOUT_S = 6.0
MODEL_PERSIST_TIMEOUT_S = 2.0
MODEL_RESTORE_INTERVAL_S = 0.2
READY_QUIET_S = 0.75
SELECT_TIMEOUT_S = 0.2
PASTE_SUBMIT_DELAY_S = 0.3
# Covers the hook CLI cold start (~1.4s measured) before it reports the submit.
SUBMIT_VERIFY_S = 4.0
MAX_SUBMIT_ATTEMPTS = 3
READY_TIMEOUT_S = 10.0
ESC_GAP_S = 0.3
# Ctrl-U: Claude Code deletes back to the start of the current screen line.
CLEAR_LINE = b"\x15"
DEFAULT_COLUMNS = 80
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]+$")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_INPUT_ESCAPE_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|O[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def strip_ansi(data: bytes) -> str:
    return _ANSI_RE.sub(b"", data).decode("utf-8", "replace")


def _squash(text: str) -> str:
    return "".join(text.split())


def _match_text(data: bytes) -> str:
    return _squash(strip_ansi(data))


def valid_model_name(name: object) -> bool:
    """Return whether *name* is safe to type as one slash-command argument."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_MODEL_NAME_LEN
        and bool(_MODEL_NAME_RE.fullmatch(name))
    )


@dataclass(frozen=True)
class FirstPromptRoute:
    """The model switch and user-facing label for a first-prompt route."""

    model: str
    display_model: str
    rationale: str


class ConfirmationState:
    """Detect and accept Claude's optional cache-cost confirmation dialog."""

    PROMPT_MARKERS = ("Switch model?", "Yes, switch to", "No, go back")

    def __init__(self, window: int = 4096) -> None:
        self._buf = ""
        self._armed_until = 0.0
        self._window = window

    def arm(self, deadline: float) -> None:
        self._armed_until = deadline
        self._buf = ""

    def clear(self) -> None:
        self._armed_until = 0.0
        self._buf = ""

    def observe(self, chunk: bytes, now: float) -> bytes | None:
        if self._armed_until == 0.0:
            return None
        if now > self._armed_until:
            self.clear()
            return None
        self._buf = (self._buf + _match_text(chunk))[-self._window :]
        if all(_squash(marker) in self._buf for marker in self.PROMPT_MARKERS):
            self.clear()
            return b"\r"
        return None


class OutputMarkerDetector:
    """Latching ANSI-insensitive substring detector."""

    def __init__(self, markers: tuple[str, ...], window: int = 4096) -> None:
        self._markers = markers
        self._buf = ""
        self._window = window
        self.triggered = False

    def observe(self, chunk: bytes) -> bool:
        if self.triggered:
            return True
        self._buf = (self._buf + _match_text(chunk))[-self._window :]
        if any(_squash(marker) in self._buf for marker in self._markers):
            self.triggered = True
        return self.triggered


class ModelSwitchInput:
    """Distinguish edits and possible model commands from terminal traffic."""

    def __init__(self) -> None:
        self._pending = b""
        self._line = b""
        self._line_known = True
        self._last_input = 0.0
        self._pasting = False

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def observe(self, data: bytes, now: float) -> tuple[bool, bool]:
        if data:
            self._pending += data
            self._last_input = now
        edited = selected_model = False
        while self._pending:
            if self._pending.startswith(b"\x1b"):
                match = _INPUT_ESCAPE_RE.match(self._pending)
                if match:
                    sequence = match[0]
                    self._pending = self._pending[match.end() :]
                    if sequence in (b"\x1b[200~", b"\x1b[201~"):
                        self._pasting = sequence == b"\x1b[200~"
                    elif sequence.startswith(b"\x1bO") or sequence[-1:] in (
                        b"A",
                        b"B",
                        b"C",
                        b"D",
                        b"F",
                        b"H",
                    ):
                        # History and cursor motion leave the submitted text unknown,
                        # but navigation alone must not interrupt automatic replay.
                        self._line_known = False
                    elif sequence.startswith(b"\x1b]") or sequence[-1:] in (
                        b"I",
                        b"O",
                        b"c",
                        b"R",
                        b"n",
                    ):
                        pass  # Focus and terminal replies do not edit the prompt.
                    else:
                        edited = True
                        self._line_known = False
                    continue
                if (
                    len(self._pending) <= 4096
                    and now - self._last_input < ESC_GAP_S
                    and (len(self._pending) == 1 or self._pending[1:2] in (b"[", b"O", b"]"))
                ):
                    break  # An escape sequence may span several stdin reads.
            char, self._pending = self._pending[:1], self._pending[1:]
            edited = True
            if char in (b"\r", b"\n") and not self._pasting:
                selected_model |= not self._line_known or (
                    b"\n" not in self._line
                    and b"\r" not in self._line
                    and self._line.lstrip().startswith(b"/")
                )
                self._line = b""
                self._line_known = True
            elif self._pasting:
                self._line = (self._line + char)[-4096:]
            elif char in (b"\x08", b"\x7f"):
                self._line = self._line[:-1]
            elif char == CLEAR_LINE:
                self._line = b""
            elif char >= b" ":
                self._line = (self._line + char)[-4096:]
            else:
                # Completion, search, and editing bindings can change text without
                # sending its contents back through stdin.
                self._line_known = False
        return edited, selected_model


def terminal_columns(fd: int) -> int:
    """Return the terminal width of *fd*, or DEFAULT_COLUMNS when it is unknown."""
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return DEFAULT_COLUMNS
    columns = struct.unpack("HHHH", packed)[1]
    return columns or DEFAULT_COLUMNS


def clear_input_keys(text: str, columns: int) -> bytes:
    """Return enough Ctrl-U presses to empty Claude Code's input box holding *text*.

    Claude Code keeps a hook-blocked prompt in the input box. Each Ctrl-U deletes
    one screen line, and one more press joins each line break, so the count covers
    every wrapped line twice. Wide characters are counted as two columns. Extra
    presses on an empty box are no-ops.
    """
    width = max(columns - 4, 20)
    rows = sum(max(1, -(-(2 * len(line) + 2) // width)) for line in text.split("\n"))
    return CLEAR_LINE * (2 * rows + 1)


def inject_model_switch(master_fd: int, model: str, *, clear: bytes = b"") -> None:
    """Type Claude Code's direct, persistent model command into an emptied input box."""
    if not valid_model_name(model):
        raise ValueError(f"Unsafe Claude model name: {model!r}")
    os.write(master_fd, clear + f"/model {model}\r".encode())


def inject_prompt(master_fd: int, prompt: str, *, submit: bool = True, clear: bytes = b"") -> None:
    """Replay a captured prompt as one bracketed paste, after the *clear* keystrokes.

    The replay loop pastes with ``submit=False`` and sends Enter after
    PASTE_SUBMIT_DELAY_S: a CR bundled with the paste close can be absorbed.
    """
    clean = prompt.replace("\r\n", "\n").replace("\r", "\n")
    clean = clean.replace("\x00", "").replace("\x1b", "")
    suffix = b"\r" if submit else b""
    os.write(master_fd, clear + b"\x1b[200~" + clean.encode() + b"\x1b[201~" + suffix)


def inject_note(out_fd: int, message: str) -> None:
    os.write(out_fd, ("\r\n\x1b[36m" + message + "\x1b[0m\r\n").encode())


def request_first_prompt_route(path: Path, payload: dict, *, timeout: float = 5.0) -> dict | None:
    """Send a UserPromptSubmit payload to the owning PTY wrapper."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    request = {
        "method": "route_first_prompt",
        "prompt": prompt,
        "session_id": payload.get("session_id"),
    }
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        with client:
            client.connect(str(path))
            client.sendall((json.dumps(request) + "\n").encode())
            with client.makefile("rb") as stream:
                raw = stream.readline()
        response = json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None
    return response if isinstance(response, dict) else None


def first_prompt_hook_output(response: dict | None) -> dict | None:
    """Translate the wrapper response into Claude hook output."""
    from ucode.smart_routing.v2 import format_routing_notice

    if not isinstance(response, dict) or response.get("action") != "block":
        return None
    model = response.get("model")
    if not valid_model_name(model):
        return None
    assert isinstance(model, str)
    display_model = response.get("display_model", model)
    if not isinstance(display_model, str) or not display_model:
        display_model = model
    rationale = response.get("rationale")
    return {
        "decision": "block",
        "reason": format_routing_notice(
            display_model, rationale if isinstance(rationale, str) else None
        ),
    }


def serve_first_prompt_socket(
    path: Path,
    route_prompt: Callable[[str], FirstPromptRoute],
    on_blocked_prompt: Callable[[str, str], None],
    stop: threading.Event,
    *,
    on_submitted_prompt: Callable[[str], None] = lambda _prompt: None,
    log: Callable[[str], None] = lambda _message: None,
) -> threading.Thread:
    """Serve the hook protocol, blocking exactly one non-command prompt."""

    def serve() -> None:
        claimed = False
        try:
            path.unlink(missing_ok=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(4)
            server.settimeout(0.5)
        except OSError as exc:
            log(f"[ERR] first-prompt socket bind failed: {exc!r}")
            return
        log(f"[READY] first-prompt socket {path}")
        try:
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn, conn.makefile("rwb") as stream:
                    response: dict = {"action": "allow"}
                    blocked: tuple[str, str] | None = None
                    submitted: str | None = None
                    try:
                        request = json.loads(stream.readline())
                        prompt = request.get("prompt") if isinstance(request, dict) else None
                        is_route = (
                            isinstance(request, dict)
                            and request.get("method") == "route_first_prompt"
                        )
                        is_command = isinstance(prompt, str) and prompt.lstrip().startswith("/")
                        if (
                            is_route
                            and not claimed
                            and isinstance(prompt, str)
                            and prompt.strip()
                            and not is_command
                        ):
                            routed = route_prompt(prompt)
                            model = routed.model
                            display_model = routed.display_model
                            rationale = routed.rationale
                            if valid_model_name(model):
                                claimed = True
                                response = {
                                    "action": "block",
                                    "model": model,
                                    "rationale": rationale,
                                }
                                response["display_model"] = display_model
                                blocked = (prompt, model)
                        elif (
                            is_route
                            and claimed
                            and not is_command
                            and isinstance(prompt, str)
                            and prompt.strip()
                        ):
                            # Replayed prompt was submitted; UserPromptSubmit hook fires again.
                            submitted = prompt
                    except Exception as exc:  # noqa: BLE001 - hooks must fail open
                        log(f"[ERR] first-prompt request: {exc!r}")
                    stream.write((json.dumps(response) + "\n").encode())
                    stream.flush()
                    if blocked is not None:
                        on_blocked_prompt(*blocked)
                    if submitted is not None:
                        on_submitted_prompt(submitted)
        finally:
            server.close()

    thread = threading.Thread(target=serve, name="claude-first-prompt", daemon=True)
    thread.start()
    return thread


class TerminalModeGuard:
    """Put stdin in raw mode for the PTY session and restore it on exit."""

    def __init__(self, fd: int = 0) -> None:
        self.fd = fd
        self._saved: list | None = None

    def __enter__(self) -> TerminalModeGuard:
        if os.isatty(self.fd):
            self._saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None


def sync_winsize(master_fd: int, stdin_fd: int = 0) -> None:
    if not os.isatty(stdin_fd):
        return
    try:
        packed = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def run_claude_pty(
    argv: list[str],
    *,
    route_prompt: Callable[[str], FirstPromptRoute],
    socket_path: Path,
    prepare_model_switch: Callable[[str], bool | None] = lambda _model: None,
    model_switch_persisted: Callable[[], bool] = lambda: True,
    restore_model_setting: Callable[[], bool | None] = lambda: None,
    defer_model_setting: Callable[[], None] = lambda: None,
    abandon_model_setting: Callable[[], bool | None] = lambda: None,
    log_path: Path | None = None,
) -> int:
    """Run Claude in a PTY, switch its model, and replay the first prompt."""

    def log(message: str) -> None:
        if log_path is None:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")
        except OSError:
            pass

    debug = os.environ.get("UCODE_CLAUDE_PTY_DEBUG") == "1"
    confirm = ConfirmationState()
    stop = threading.Event()
    pending_lock = threading.Lock()
    pending: dict[str, tuple[str, str] | None] = {"value": None}
    # Set when the UserPromptSubmit hook fires after the claim, confirming submission.
    submitted_event = threading.Event()

    def on_blocked_prompt(prompt: str, model: str) -> None:
        with pending_lock:
            pending["value"] = (prompt, model)
        log(f"[ROUTE] first prompt -> {model!r}")

    def on_submitted_prompt(_prompt: str) -> None:
        submitted_event.set()

    server_thread = serve_first_prompt_socket(
        socket_path,
        route_prompt,
        on_blocked_prompt,
        stop,
        on_submitted_prompt=on_submitted_prompt,
        log=log,
    )
    socket_deadline = time.monotonic() + 2.0
    while (
        not socket_path.exists() and server_thread.is_alive() and time.monotonic() < socket_deadline
    ):
        time.sleep(0.01)
    if not socket_path.exists():
        log("[ERR] first-prompt socket was not ready before Claude launch")
        stop.set()
        raise RuntimeError(
            "Smart routing could not start its local prompt-routing socket; Claude was not launched."
        )

    pid, master_fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
        os._exit(127)

    previous_winch = signal.getsignal(signal.SIGWINCH)

    def on_winch(_signum: int, _frame: object) -> None:
        sync_winsize(master_fd)

    try:
        with TerminalModeGuard(0):
            signal.signal(signal.SIGWINCH, on_winch)
            sync_winsize(master_fd)
            stdin_open = True
            last_output = 0.0
            phase = "waiting_prompt"
            routed_prompt = ""
            routed_model = ""
            switch_started = 0.0
            ready_capture = 0.0
            paste_deadline = 0.0
            verify_deadline = 0.0
            esc_deadline = 0.0
            submit_attempts = 0
            switch_complete: OutputMarkerDetector | None = None
            switch_cancelled: OutputMarkerDetector | None = None
            model_pending = False
            deferred_restore = False
            restore_ready = False
            next_restore_check = 0.0
            restore_error_logged = False
            user_input = ModelSwitchInput()

            def observe_input(data: bytes, now: float) -> None:
                nonlocal phase, model_pending, deferred_restore, restore_ready
                if not model_pending and phase not in ("waiting_to_switch", "pasting", "verifying"):
                    return
                if phase == "verifying" and submitted_event.is_set():
                    return
                edited, selected_model = user_input.observe(data, now)
                if not edited:
                    return
                if phase != "done":
                    already_submitted = phase == "verifying"
                    phase = "done"
                    confirm.clear()
                    inject_note(
                        1,
                        "Smart Routing stopped automatic replay because you entered input. "
                        + (
                            "Check whether your prompt was sent before resending it."
                            if already_submitted
                            else "Resend your prompt when Claude is ready."
                        ),
                    )
                    log("[WARN] user input stopped automatic replay")
                if model_pending:
                    if selected_model:
                        # Unknown submitted text can select a model through history
                        # or completion, including the routed model itself.
                        try:
                            model_pending = abandon_model_setting() is False
                        except (OSError, ValueError, RuntimeError) as exc:
                            model_pending = True
                            log(f"[WARN] manual model handoff deferred: {exc}")
                        deferred_restore = model_pending
                        restore_ready = model_pending
                    else:
                        defer_model_setting()
                        deferred_restore = True

            def clear_keys() -> bytes:
                # The box can still hold the blocked prompt and an unrun /model command.
                return clear_input_keys(
                    f"{routed_prompt}/model {routed_model}", terminal_columns(master_fd)
                )

            while True:
                readable = [master_fd, 0] if stdin_open else [master_fd]
                try:
                    ready_fds, _, _ = select.select(readable, [], [], SELECT_TIMEOUT_S)
                except InterruptedError:
                    continue

                if 0 in ready_fds:
                    try:
                        data = os.read(0, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        stdin_open = False
                    else:
                        observe_input(data, time.monotonic())
                        os.write(master_fd, data)

                if master_fd in ready_fds:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    os.write(1, chunk)
                    last_output = time.monotonic()
                    if debug:
                        log(f"[OUT] {strip_ansi(chunk)[:400]!r}")
                    keystroke = confirm.observe(chunk, last_output)
                    if keystroke is not None:
                        os.write(master_fd, keystroke)
                    if phase in ("switching", "waiting_cancel") and switch_complete is not None:
                        switch_complete.observe(chunk)
                        assert switch_cancelled is not None
                        if phase == "waiting_cancel":
                            switch_cancelled.observe(chunk)

                if phase == "waiting_prompt":
                    with pending_lock:
                        captured = pending["value"]
                    if captured is not None:
                        routed_prompt, routed_model = captured
                        phase = "waiting_to_switch"
                        ready_capture = time.monotonic()

                now = time.monotonic()
                observe_input(b"", now)
                idle = last_output > 0.0 and now - last_output >= READY_QUIET_S
                if (
                    model_pending
                    and (deferred_restore or phase == "waiting_persist")
                    and now >= next_restore_check
                ):
                    next_restore_check = now + MODEL_RESTORE_INTERVAL_S
                    try:
                        if (
                            restore_ready or model_switch_persisted()
                        ) and restore_model_setting() is not False:
                            model_pending = False
                            deferred_restore = False
                            log("[RESTORE] saved model restored")
                    except (OSError, ValueError, RuntimeError) as exc:
                        defer_model_setting()
                        if not restore_error_logged:
                            log(f"[WARN] saved model restoration deferred: {exc}")
                            restore_error_logged = True
                if user_input.pending:
                    continue  # Do not inject keystrokes into an incomplete terminal reply.
                if (
                    phase == "waiting_to_switch"
                    and idle
                    and prepare_model_switch(routed_model) is not False
                ):
                    model_pending = True
                    inject_model_switch(master_fd, routed_model, clear=clear_keys())
                    confirm.arm(now + CONFIRM_TIMEOUT_S)
                    switch_complete = OutputMarkerDetector(("Set model to", "Model set to"))
                    switch_cancelled = OutputMarkerDetector(("Kept model as",))
                    switch_started = now
                    phase = "switching"
                    log(f"[SWITCH] /model {routed_model}")
                elif phase == "waiting_to_switch" and now - ready_capture >= READY_TIMEOUT_S:
                    # Neither terminal activity nor another session's lock may stall input.
                    inject_note(
                        1,
                        "Smart Routing could not prepare the model switch; "
                        "submitting your prompt on the default model.",
                    )
                    inject_prompt(master_fd, routed_prompt, submit=False, clear=clear_keys())
                    paste_deadline = now + PASTE_SUBMIT_DELAY_S
                    phase = "pasting"
                    log("[WARN] waiting_to_switch timed out; skipping /model, pasting prompt")
                elif (
                    phase in ("switching", "waiting_cancel")
                    and now >= esc_deadline
                    and switch_complete is not None
                    and switch_cancelled is not None
                    and (switch_complete.triggered or switch_cancelled.triggered)
                ):
                    # Claude's explicit "Kept model as" result completes cancellation;
                    # a generic cancel message or quiet redraw does not.
                    restore_ready = switch_cancelled.triggered and not switch_complete.triggered
                    phase = "waiting_persist"
                    switch_started = now
                elif phase == "switching" and now - switch_started >= SWITCH_TIMEOUT_S:
                    # ESC can race with /model finishing. Replaying before its final
                    # redraw lets the command clear our pasted prompt.
                    os.write(master_fd, b"\x1b")
                    confirm.clear()
                    inject_note(
                        1,
                        "Smart Routing could not confirm the model switch; "
                        "waiting for Claude before replaying your prompt.",
                    )
                    switch_started = now
                    esc_deadline = now + ESC_GAP_S
                    phase = "waiting_cancel"
                    log(f"[ERR] direct model switch timed out for {routed_model!r}")
                elif phase == "waiting_cancel" and now - switch_started >= READY_TIMEOUT_S:
                    # A quiet redraw does not prove /model has stopped writing.
                    defer_model_setting()
                    deferred_restore = True
                    inject_note(
                        1,
                        "Smart Routing could not confirm Claude was ready. "
                        "Resend your prompt when Claude is ready.",
                    )
                    phase = "done"
                    log("[WARN] Claude did not settle after Escape; automatic replay stopped")
                elif phase == "waiting_persist" and not model_pending:
                    inject_prompt(master_fd, routed_prompt, submit=False, clear=clear_keys())
                    paste_deadline = now + PASTE_SUBMIT_DELAY_S
                    phase = "pasting"
                    log("[PASTE] first prompt pasted; waiting for submit delay")
                elif phase == "waiting_persist" and now - switch_started >= MODEL_PERSIST_TIMEOUT_S:
                    # Fail-open: don't kill claude; submit on whatever model is active.
                    defer_model_setting()
                    deferred_restore = True
                    inject_note(
                        1,
                        "Smart Routing could not confirm the default model was preserved. "
                        "Check /model if needed.",
                    )
                    inject_prompt(master_fd, routed_prompt, submit=False, clear=clear_keys())
                    paste_deadline = now + PASTE_SUBMIT_DELAY_S
                    phase = "pasting"
                    log("[WARN] saved model restoration still pending; submitting prompt")
                elif phase == "pasting" and now >= paste_deadline:
                    os.write(master_fd, b"\r")
                    verify_deadline = now + SUBMIT_VERIFY_S
                    submit_attempts = 1
                    phase = "verifying"
                    log("[SUBMIT] Enter sent; verifying submission")
                elif phase == "verifying":
                    if submitted_event.is_set():
                        phase = "done"
                        log("[DONE] first prompt confirmed submitted")
                    elif now >= verify_deadline:
                        if submit_attempts >= MAX_SUBMIT_ATTEMPTS:
                            inject_note(
                                1,
                                "Smart Routing could not confirm your prompt was submitted. "
                                "Check that it was sent.",
                            )
                            phase = "done"
                            log("[WARN] prompt submit unconfirmed after max attempts")
                        else:
                            os.write(master_fd, b"\r")
                            submit_attempts += 1
                            verify_deadline = now + SUBMIT_VERIFY_S
                            log(f"[RETRY] resending Enter (attempt {submit_attempts})")
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        stop.set()
        with contextlib.suppress(OSError):
            os.close(master_fd)
        socket_path.unlink(missing_ok=True)

    _waited_pid, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1
