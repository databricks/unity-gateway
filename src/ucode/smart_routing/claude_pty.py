"""PTY wrapper that routes Claude Code's first prompt with ``/model <name>``.

The gateway TUI's plugin API exposes no session model setter, so a per-launch
UserPromptSubmit hook captures and blocks the first prompt. While the TUI is idle,
this wrapper types a direct model command, restores the user's persisted default
model, and replays the prompt. Claude Code keeps a blocked prompt in its input box, so every
write into the box first clears it. Terminal traffic is otherwise forwarded unchanged.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
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
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from ucode import databricks, metrics
from ucode.config_io import APP_DIR
from ucode.os_compatibility.file_lock_cross_os import acquire_exclusive_file_lock, release_file_lock
from ucode.smart_routing.v2 import ClaudeRoutingSetupError, format_routing_notice

MAX_MODEL_NAME_LEN = 200
MAX_PROMPT_RECOVERY_FILES = 10
CONFIRM_TIMEOUT_S = 3.0
SWITCH_TIMEOUT_S = 6.0
MODEL_PERSIST_TIMEOUT_S = 2.0
# Leave the receipt helper time to receive a refusal before its five-second socket timeout.
MODEL_SETTING_RECEIPT_TIMEOUT_S = 4.0
MODEL_PREPARE_INTERVAL_S = 0.2
MODEL_RESTORE_INTERVAL_S = 0.2
MODEL_RESTORE_MAX_INTERVAL_S = 2.0
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
# Claude Code reads 64 or more bytes that arrive together as a paste, which drops
# Ctrl-U and keeps Enter as text, so typed keys go out in smaller writes.
KEYSTROKE_CHUNK_BYTES = 16
KEYSTROKE_GAP_S = 0.02
# A stalled Claude reads the clear's unread tail and the next keys as one burst, so the
# next keys wait until Claude's output has been quiet this long after the clear.
CLEAR_SETTLE_S = 0.25
CLEAR_SETTLE_TIMEOUT_S = 1.0
DEFAULT_COLUMNS = 80
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/\-\[\]]+$")
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_INPUT_ESCAPE_RE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|O[@-~]|\][^\x07]*(?:\x07|\x1b\\)|P[^\x1b]*\x1b\\)"
)
_TERMINAL_REPLY_RE = re.compile(
    rb"\x1b(?:\[\?[0-9]+u|\[\??[0-9]+;[0-4]\$y|P(?:>\||[01][$+]r)[ -~]*\x1b\\)"
)
_MODEL_SWITCH_COMMANDS = ("/model", "/config", "/settings", "/fast", "/auto")


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


class ManualModelConfirmation:
    """Match a completed native model result after a possible manual command."""

    def __init__(self, model: str | None) -> None:
        self._model = _squash(model).casefold() if model is not None else None
        self._buffer = b""

    def _matches_model(self, label: str) -> bool:
        if not label:
            return False
        if self._model is None or label == self._model:
            return True
        # Native family shortcuts render a display version instead of the saved alias.
        return (
            self._model in databricks.ANTHROPIC_FAMILIES
            and re.fullmatch(rf"(?:claude)?{self._model}(?:[0-9]+(?:\.[0-9]+)*)?", label)
            is not None
        )

    def observe(self, chunk: bytes) -> Literal["confirmed", "cancelled"] | None:
        self._buffer = (self._buffer + chunk)[-8192:]
        text = re.sub(r"[^\S\r\n]+", "", strip_ansi(self._buffer)).casefold()
        prefix = r"(?:^|[\r\n])(?:⎿)?"
        rejected = re.search(
            prefix + r"modelswitchto(.+?)wasblockedbyapremodelswitchhook(?::[^\r\n]*)?[\r\n]",
            text,
        )
        if rejected and self._matches_model(rejected[1]):
            return "cancelled"
        if re.search(prefix + r"modelswitchcancelled[\r\n]", text):
            return "cancelled"
        for result in re.finditer(
            prefix + r"(setmodelto|modelsetto|keptmodelas)(.*?)"
            r"(andsavedasyourdefaultfornewsessions|forthissessiononly|[\r\n])",
            text,
        ):
            if result[1] == "keptmodelas":
                return "cancelled"
            if self._matches_model(result[2]):
                return "cancelled" if result[3] == "forthissessiononly" else "confirmed"
        return None


@dataclass(frozen=True)
class ModelSwitchInputResult:
    edited: bool
    model_command: bool
    manual_model: str | None = None


class ModelSwitchInput:
    """Distinguish edits and possible model commands from terminal traffic."""

    def __init__(self) -> None:
        self._pending = b""
        self._line = b""
        self._cursor = 0
        self._line_known = True
        self._last_input = 0.0
        self._pasting = False

    @property
    def pending(self) -> bool:
        return bool(self._pending) or self._pasting

    def _move_cursor(self, direction: bytes, count: int = 1) -> None:
        if (
            not self._line_known
            or not self._line
            or b"\n" in self._line
            or not self._line.isascii()
        ):
            # Native cursor movement counts graphemes and visual rows.
            self._line_known = False
            return
        if direction == b"D":
            self._cursor = max(0, self._cursor - count)
        elif direction == b"C":
            self._cursor = min(len(self._line), self._cursor + count)
        else:
            self._cursor = 0 if direction == b"H" else len(self._line)

    def observe(self, data: bytes, now: float) -> ModelSwitchInputResult:
        if data:
            self._pending += data
            self._last_input = now
        edited = model_command = False
        manual_model = None
        while self._pending:
            if self._pending.startswith(b"\x1b"):
                match = _INPUT_ESCAPE_RE.match(self._pending)
                if match:
                    sequence = match[0]
                    self._pending = self._pending[match.end() :]
                    if sequence in (b"\x1b[200~", b"\x1b[201~"):
                        self._pasting = sequence == b"\x1b[200~"
                        edited |= self._pasting
                    elif cursor := re.fullmatch(rb"\x1b(?:\[([0-9]*)|O)([CDHF])", sequence):
                        count = cursor[1] or b"1"
                        if len(count) > 4:
                            self._line_known = False
                        else:
                            self._move_cursor(cursor[2], max(1, int(count)))
                    elif sequence == b"\x1b[3~":
                        start = self._cursor
                        self._move_cursor(b"C")
                        self._line = self._line[:start] + self._line[self._cursor :]
                        self._cursor = start
                        edited = True
                    elif (
                        sequence.startswith(b"\x1bO")
                        or sequence in (b"\x1b[1~", b"\x1b[4~")
                        or sequence[-1:] in (b"A", b"B", b"C", b"D", b"F", b"H")
                    ):
                        # History and cursor motion leave the submitted text unknown,
                        # but navigation alone must not interrupt automatic replay.
                        self._line_known = False
                    elif (
                        _TERMINAL_REPLY_RE.fullmatch(sequence)
                        or sequence.startswith(b"\x1b]")
                        or sequence[-1:]
                        in (
                            b"I",
                            b"O",
                            b"c",
                            b"R",
                            b"n",
                        )
                    ):
                        pass  # Focus and terminal replies do not edit the prompt.
                    else:
                        edited = True
                        self._line_known = False
                    continue
                if (
                    len(self._pending) <= 4096
                    and now - self._last_input < ESC_GAP_S
                    and (len(self._pending) == 1 or self._pending[1:2] in (b"[", b"O", b"]", b"P"))
                ):
                    break  # An escape sequence may span several stdin reads.
            char, self._pending = self._pending[:1], self._pending[1:]
            edited = True
            if char in (b"\r", b"\n") and not self._pasting:
                command = self._line.decode("utf-8", "replace").split()
                possible_model = not self._line_known or (
                    b"\n" not in self._line
                    and b"\r" not in self._line
                    and bool(command)
                    # Enter may accept a partially typed command from native completion.
                    and any(name.startswith(command[0]) for name in _MODEL_SWITCH_COMMANDS)
                )
                if possible_model:
                    model_command = True
                    manual_model = None
                    if (
                        self._line_known
                        and len(command) == 2
                        and command[0] == "/model"
                        and valid_model_name(command[1])
                    ):
                        manual_model = command[1]
                self._line = b""
                self._cursor = 0
                self._line_known = True
            elif self._pasting:
                self._line_known &= len(self._line) < 4096
                self._line = (self._line[: self._cursor] + char + self._line[self._cursor :])[
                    -4096:
                ]
                self._cursor = min(self._cursor + 1, len(self._line))
            elif char in (b"\x08", b"\x7f"):
                end = self._cursor
                self._move_cursor(b"D")
                self._line = self._line[: self._cursor] + self._line[end:]
            elif char == CLEAR_LINE:
                self._line = self._line[self._cursor :]
                self._cursor = 0
                self._line_known = False  # Native Ctrl-U may stop at a wrapped screen row.
            elif char >= b" ":
                self._line_known &= len(self._line) < 4096
                self._line = (self._line[: self._cursor] + char + self._line[self._cursor :])[
                    -4096:
                ]
                self._cursor = min(self._cursor + 1, len(self._line))
            else:
                # Completion, search, and editing bindings can change text without
                # sending its contents back through stdin.
                self._line_known = False
        return ModelSwitchInputResult(
            edited=edited, model_command=model_command, manual_model=manual_model
        )


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


def model_switch_keys(model: str) -> bytes:
    """Return the keys of Claude Code's direct, persistent model command."""
    if not valid_model_name(model):
        raise ValueError(f"Unsafe Claude model name: {model!r}")
    return f"/model {model}\r".encode()


def inject_prompt(master_fd: int, prompt: str, *, submit: bool = True) -> None:
    """Replay a captured prompt as one bracketed paste.

    The replay loop pastes with ``submit=False`` and sends Enter after
    PASTE_SUBMIT_DELAY_S: a CR bundled with the paste close can be absorbed.
    """
    clean = prompt.replace("\r\n", "\n").replace("\r", "\n")
    clean = clean.replace("\x00", "").replace("\x1b", "")
    suffix = b"\r" if submit else b""
    os.write(master_fd, b"\x1b[200~" + clean.encode() + b"\x1b[201~" + suffix)


def inject_note(out_fd: int, message: str) -> None:
    os.write(out_fd, ("\r\n\x1b[36m" + message + "\x1b[0m\r\n").encode())


def prompt_recovery_note(prompt: str) -> str:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        with os.fdopen(
            os.open(APP_DIR / "claude-prompt-recovery.lock", flags, 0o600),
            "a+",
            encoding="utf-8",
        ) as lock:
            acquire_exclusive_file_lock(lock, timeout=0.1)
            try:
                copies = islice(APP_DIR.glob("claude-prompt-*.txt"), MAX_PROMPT_RECOVERY_FILES)
                if sum(1 for _ in copies) >= MAX_PROMPT_RECOVERY_FILES:
                    return (
                        f"The recovery file limit ({MAX_PROMPT_RECOVERY_FILES}) was reached in "
                        f"{APP_DIR}. Delete copies you have already resent. "
                        f"Your prompt as a JSON string: {json.dumps(prompt)}"
                    )
                new_copy: Path | None = None
                try:
                    with NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        newline="",
                        prefix="claude-prompt-",
                        suffix=".txt",
                        dir=APP_DIR,
                        delete=False,
                    ) as saved:
                        new_copy = Path(saved.name)
                        saved.write(prompt)
                        saved.flush()
                        os.fsync(saved.fileno())
                except OSError:
                    if new_copy is not None:
                        with contextlib.suppress(OSError):
                            new_copy.unlink()
                    raise
            finally:
                release_file_lock(lock)
        return (
            f"Your prompt is saved in {saved.name} until you delete it. "
            "Open it to resend when Claude is ready, then delete the recovery file."
        )
    except OSError:
        # Escaping control bytes keeps the recovery copy from acting on the terminal.
        return f"Could not save a recovery file. Your prompt as a JSON string: {json.dumps(prompt)}"


def request_first_prompt_route(
    path: Path, payload: dict, *, timeout: float = 5.0, route_enabled: bool = True
) -> dict | None:
    """Forward a first-prompt or clear hook to the owning PTY wrapper."""
    if payload.get("hook_event_name") == "SessionStart":
        if payload.get("source") != "clear":
            return None
        request = {"method": "session_cleared", "session_id": payload.get("session_id")}
    else:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return None
        request = {
            "method": "route_first_prompt" if route_enabled else "prompt_submitted",
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
    on_session_cleared: Callable[[], None] = lambda: None,
    on_model_setting: Callable[[dict], dict] = lambda _request: {},
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
                        is_submission = (
                            isinstance(request, dict)
                            and request.get("method") == "prompt_submitted"
                        )
                        is_command = isinstance(prompt, str) and prompt.lstrip().startswith("/")
                        if isinstance(request, dict) and request.get("method") == "model_setting":
                            response = on_model_setting(request)
                        elif (
                            isinstance(request, dict) and request.get("method") == "session_cleared"
                        ):
                            on_session_cleared()
                        elif (
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
                            (is_route and claimed or is_submission)
                            and not is_command
                            and isinstance(prompt, str)
                            and prompt.strip()
                        ):
                            # Replayed prompt was submitted; UserPromptSubmit hook fires again.
                            submitted = prompt
                    except Exception as exc:  # noqa: BLE001 - hooks must fail open
                        log(f"[ERR] first-prompt request: {exc!r}")
                    if submitted is not None:
                        # Classify the prompt before Claude can emit its reply.
                        on_submitted_prompt(submitted)
                    stream.write((json.dumps(response) + "\n").encode())
                    stream.flush()
                    if blocked is not None:
                        on_blocked_prompt(*blocked)
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
    abandon_model_setting: Callable[[str | None], bool | None] = lambda _model: None,
    confirm_manual_model_setting: Callable[[], bool | None] = lambda: None,
    prepare_saved_model_setting: Callable[[], bool | None] = lambda: None,
    confirm_saved_model_setting: Callable[[str | None], bool | None] = lambda _model: None,
    cancel_manual_model_setting: Callable[[], bool | None] = lambda: None,
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
    submitted_count = 0
    cleared_count = 0
    model_setting_requests: list[tuple[dict, Future[dict], float]] = []

    def on_blocked_prompt(prompt: str, model: str) -> None:
        with pending_lock:
            pending["value"] = (prompt, model)
        log(f"[ROUTE] first prompt -> {model!r}")

    def on_submitted_prompt(_prompt: str) -> None:
        nonlocal submitted_count
        with pending_lock:
            submitted_count += 1
            submitted_event.set()

    def on_session_cleared() -> None:
        nonlocal cleared_count
        with pending_lock:
            cleared_count += 1

    def on_model_setting(request: dict) -> dict:
        now = time.monotonic()
        deadline = request.get("deadline", now + MODEL_SETTING_RECEIPT_TIMEOUT_S)
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            return {"acknowledged": False}
        deadline = min(deadline, now + MODEL_SETTING_RECEIPT_TIMEOUT_S)
        if deadline <= now:
            return {"acknowledged": False}
        completed: Future[dict] = Future()
        with pending_lock:
            model_setting_requests.append((request, completed, deadline))
        try:
            return completed.result(timeout=max(0.0, deadline - time.monotonic()))
        except (TimeoutError, CancelledError):
            # Cancellation prevents queued work from applying after its caller gives up.
            # An operation already changing recovery must finish its write.
            completed.cancel()
            log("[WARN] native model-setting receipt was not processed before its deadline")
            return {"acknowledged": False}

    server_thread = serve_first_prompt_socket(
        socket_path,
        route_prompt,
        on_blocked_prompt,
        stop,
        on_submitted_prompt=on_submitted_prompt,
        on_session_cleared=on_session_cleared,
        on_model_setting=on_model_setting,
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
        raise ClaudeRoutingSetupError(
            "Smart routing could not start its local prompt-routing socket"
        )

    metrics.agent_handoff()
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
            next_prepare_check = 0.0
            paste_deadline = 0.0
            verify_deadline = 0.0
            esc_deadline = 0.0
            submit_attempts = 0
            switch_complete: OutputMarkerDetector | None = None
            switch_cancelled: OutputMarkerDetector | None = None
            manual_complete: ManualModelConfirmation | None = None
            manual_unknown = False
            manual_generation = 0
            automatic_command_pending = False
            automatic_command_id: int | None = None
            native_saves: dict[int, tuple[bool, int]] = {}
            manual_prompt_after: int | None = None
            manual_clear_after: int | None = None
            model_pending = False
            deferred_restore = False
            restore_ready = False
            next_restore_check = 0.0
            restore_check_interval = MODEL_RESTORE_INTERVAL_S
            restore_error_logged = False
            user_input = ModelSwitchInput()

            def observe_input(data: bytes, now: float) -> None:
                nonlocal phase, model_pending, deferred_restore, restore_ready, manual_complete
                nonlocal next_restore_check, restore_check_interval
                nonlocal manual_unknown, manual_prompt_after, manual_clear_after, manual_generation
                if phase == "waiting_prompt":
                    return
                observation = user_input.observe(data, now)
                if observation.model_command:
                    # A known manual target must also survive other sessions' late writes.
                    model = observation.manual_model
                    manual_generation += 1
                    manual_complete = ManualModelConfirmation(model)
                    manual_unknown = model is None
                    with pending_lock:
                        manual_prompt_after = submitted_count if manual_unknown else None
                        manual_clear_after = cleared_count if manual_unknown else None
                        # Skip the outstanding replay acknowledgement, then classify later hooks.
                        if (
                            manual_prompt_after is not None
                            and submit_attempts
                            and not submitted_event.is_set()
                        ):
                            manual_prompt_after += 1
                    try:
                        model_pending = abandon_model_setting(model) is False
                    except (OSError, ValueError, RuntimeError) as exc:
                        model_pending = True
                        log(f"[WARN] manual model handoff deferred: {exc}")
                    deferred_restore = model_pending
                    restore_ready = model_pending
                    next_restore_check = 0.0
                    restore_check_interval = MODEL_RESTORE_INTERVAL_S
                if phase == "verifying" and submitted_event.is_set():
                    return
                if not observation.edited:
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
                if model_pending and not observation.model_command:
                    defer_model_setting()
                    deferred_restore = True

            def settle_manual_choice(
                outcome: Literal["confirmed", "cancelled"], saved: dict | None = None
            ) -> None:
                nonlocal model_pending, deferred_restore, restore_ready, manual_complete
                nonlocal next_restore_check, restore_check_interval, manual_prompt_after
                nonlocal manual_clear_after
                try:
                    complete_manual = (
                        confirm_manual_model_setting
                        if outcome == "confirmed"
                        else cancel_manual_model_setting
                    )
                    model_pending = (
                        confirm_saved_model_setting(saved["model"])
                        if saved is not None
                        else complete_manual()
                    ) is False
                except (OSError, ValueError, RuntimeError) as exc:
                    model_pending = True
                    log(f"[WARN] {outcome} manual model handoff deferred: {exc}")
                deferred_restore = model_pending
                # Cancelling a manual choice does not acknowledge the delayed routed write.
                restore_ready = False
                next_restore_check = 0.0
                restore_check_interval = MODEL_RESTORE_INTERVAL_S
                manual_complete = None
                manual_prompt_after = None
                manual_clear_after = None

            def process_model_setting_requests() -> None:
                nonlocal automatic_command_pending, automatic_command_id
                nonlocal model_pending, deferred_restore, restore_ready, manual_complete
                nonlocal manual_unknown, manual_prompt_after, manual_clear_after
                with pending_lock:
                    requests = model_setting_requests[:]
                    model_setting_requests.clear()
                for request, completed, deadline in requests:
                    if time.monotonic() >= deadline:
                        completed.cancel()
                    if not completed.set_running_or_notify_cancel():
                        continue
                    response: dict = {}
                    try:
                        event = request.get("event")
                        receipt_id = request.get("id")
                        if (
                            type(receipt_id) is not int
                            or receipt_id <= 0
                            or event not in {"command", "prepare", "saved", "failed"}
                        ):
                            continue
                        response["acknowledged"] = True
                        if event == "command":
                            if (
                                automatic_command_pending
                                and request.get("command") == "model"
                                and request.get("origin") == "composer"
                            ):
                                # The injected command precedes any later stdin in the PTY.
                                automatic_command_id = receipt_id
                                automatic_command_pending = False
                        elif event == "prepare":
                            automatic = (
                                automatic_command_id is not None
                                and request.get("command_id") == automatic_command_id
                            )
                            native_saves[receipt_id] = (automatic, manual_generation)
                            if not automatic:
                                model_pending = prepare_saved_model_setting() is False
                                deferred_restore = restore_ready = model_pending
                                manual_unknown = True
                                manual_complete = ManualModelConfirmation(None)
                                with pending_lock:
                                    manual_prompt_after = submitted_count
                                    manual_clear_after = cleared_count
                        elif event in {"saved", "failed"}:
                            saved = native_saves.pop(receipt_id, None)
                            if saved is None or saved[0] or saved[1] != manual_generation:
                                continue
                            if event == "failed":
                                settle_manual_choice("cancelled")
                            elif "model" in request and (
                                request["model"] is None or isinstance(request["model"], str)
                            ):
                                settle_manual_choice("confirmed", {"model": request["model"]})
                    except (OSError, ValueError, RuntimeError) as exc:
                        response["acknowledged"] = False
                        model_pending = deferred_restore = True
                        log(f"[WARN] native model-setting recovery deferred: {exc}")
                    finally:
                        completed.set_result(response)

            def observe_model_output(chunk: bytes) -> None:
                nonlocal automatic_command_pending
                if phase in ("switching", "waiting_cancel") and switch_complete is not None:
                    switch_complete.observe(chunk)
                    if switch_complete.triggered:
                        automatic_command_pending = False
                    assert switch_cancelled is not None
                    if phase == "waiting_cancel":
                        switch_cancelled.observe(chunk)

                if manual_complete is not None:
                    with pending_lock:
                        ordinary_prompt = (
                            manual_prompt_after is not None
                            and submitted_count > manual_prompt_after
                        )
                        session_cleared = (
                            manual_clear_after is not None and cleared_count > manual_clear_after
                        )
                    if ordinary_prompt or session_cleared:
                        settle_manual_choice("cancelled")
                    elif chunk:
                        outcome = manual_complete.observe(chunk)
                        if outcome is not None and (outcome != "confirmed" or not manual_unknown):
                            settle_manual_choice(outcome)

            def forward_output(chunk: bytes) -> None:
                nonlocal last_output
                os.write(1, chunk)
                last_output = time.monotonic()
                if debug:
                    log(f"[OUT] {strip_ansi(chunk)[:400]!r}")
                keystroke = confirm.observe(chunk, last_output)
                if keystroke is not None:
                    os.write(master_fd, keystroke)
                observe_model_output(chunk)

            def forward_output_until(deadline: float) -> bool:
                """Forward Claude's output until *deadline*; return False once Claude exits."""
                while (remaining := deadline - time.monotonic()) > 0:
                    try:
                        ready_fds, _, _ = select.select([master_fd], [], [], remaining)
                    except InterruptedError:
                        continue
                    if not ready_fds:
                        break
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        return False
                    if not chunk:
                        return False
                    forward_output(chunk)
                return True

            def type_keys(keys: bytes) -> None:
                # Forwarding output between writes keeps Claude's redraws from stalling it,
                # which would merge the writes back into one paste.
                for start in range(0, len(keys), KEYSTROKE_CHUNK_BYTES):
                    os.write(master_fd, keys[start : start + KEYSTROKE_CHUNK_BYTES])
                    if not forward_output_until(time.monotonic() + KEYSTROKE_GAP_S):
                        return

            def clear_input() -> None:
                # The box can still hold the blocked prompt and an unrun /model command.
                type_keys(
                    clear_input_keys(
                        f"{routed_prompt}/model {routed_model}", terminal_columns(master_fd)
                    )
                )
                cleared = time.monotonic()
                settle_deadline = cleared + CLEAR_SETTLE_TIMEOUT_S
                while True:
                    quiet_at = min(max(cleared, last_output) + CLEAR_SETTLE_S, settle_deadline)
                    if quiet_at <= time.monotonic() or not forward_output_until(quiet_at):
                        return

            def paste_prompt() -> None:
                clear_input()
                inject_prompt(master_fd, routed_prompt, submit=False)

            while True:
                process_model_setting_requests()
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

                chunk = b""
                if master_fd in ready_fds:
                    try:
                        chunk = os.read(master_fd, 8192)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    forward_output(chunk)
                else:
                    observe_model_output(b"")

                if phase == "waiting_prompt":
                    with pending_lock:
                        captured = pending["value"]
                    if captured is not None:
                        routed_prompt, routed_model = captured
                        phase = "waiting_to_switch"
                        ready_capture = time.monotonic()

                now = time.monotonic()
                observe_input(b"", now)
                if phase == "verifying" and submitted_event.is_set():
                    phase = "done"
                    log("[DONE] first prompt confirmed submitted")
                idle = last_output > 0.0 and now - last_output >= READY_QUIET_S
                if (
                    model_pending
                    and (deferred_restore or phase == "waiting_persist")
                    and now >= next_restore_check
                ):
                    next_restore_check = now + restore_check_interval
                    if deferred_restore:
                        # Keep recovering late writes without polling at startup frequency.
                        restore_check_interval = min(
                            restore_check_interval * 2, MODEL_RESTORE_MAX_INTERVAL_S
                        )
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
                switch_prepared = False
                if (
                    phase == "waiting_to_switch"
                    and idle
                    and now >= next_prepare_check
                ):
                    switch_prepared = prepare_model_switch(routed_model) is not False
                    next_prepare_check = time.monotonic() + MODEL_PREPARE_INTERVAL_S
                if switch_prepared:
                    model_pending = True
                    automatic_command_pending = True
                    clear_input()
                    confirm.arm(time.monotonic() + CONFIRM_TIMEOUT_S)
                    switch_complete = OutputMarkerDetector(("Set model to", "Model set to"))
                    switch_cancelled = OutputMarkerDetector(("Kept model as",))
                    phase = "switching"
                    type_keys(model_switch_keys(routed_model))
                    switch_started = time.monotonic()
                    log(f"[SWITCH] /model {routed_model}")
                elif phase == "waiting_to_switch" and now - ready_capture >= READY_TIMEOUT_S:
                    # Neither terminal activity nor another session's lock may stall input.
                    inject_note(
                        1,
                        "Smart Routing could not prepare the model switch; "
                        "submitting your prompt on the default model.",
                    )
                    paste_prompt()
                    paste_deadline = time.monotonic() + PASTE_SUBMIT_DELAY_S
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
                        + prompt_recovery_note(routed_prompt),
                    )
                    phase = "done"
                    log("[WARN] Claude did not settle after Escape; automatic replay stopped")
                elif phase == "waiting_persist" and not model_pending:
                    paste_prompt()
                    paste_deadline = time.monotonic() + PASTE_SUBMIT_DELAY_S
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
                    paste_prompt()
                    paste_deadline = time.monotonic() + PASTE_SUBMIT_DELAY_S
                    phase = "pasting"
                    log("[WARN] saved model restoration still pending; submitting prompt")
                elif phase == "pasting" and now >= paste_deadline:
                    os.write(master_fd, b"\r")
                    verify_deadline = now + SUBMIT_VERIFY_S
                    submit_attempts = 1
                    phase = "verifying"
                    log("[SUBMIT] Enter sent; verifying submission")
                elif phase == "verifying":
                    if now >= verify_deadline:
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
