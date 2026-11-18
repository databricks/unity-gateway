"""Terminal Auto chooser for Claude releases before native hook modules.

The parent observes only native /model command records, never assistant prose.
Claude owns model validation and cache warnings. Routing and the saved preference
change only after Claude confirms the requested real model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ucode import metrics
from ucode.config_io import APP_DIR, atomic_write_json, read_json_safe
from ucode.constants import SMART_ROUTING_ENV_KEYS
from ucode.os_compatibility import subprocess_cross_os
from ucode.ui import prompt_for_selection

_COMMAND = re.compile(
    r"^<command-name>/model</command-name>\s*"
    r"<command-message>model</command-message>\s*<command-args>([^<]*)</command-args>$"
)
_OUTPUT = re.compile(r"^<local-command-stdout>(.*?)</local-command-stdout>$", re.DOTALL)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def session_hook(path: Path, payload: dict) -> None:
    """Publish this launch's transcript cursor, including after clear/resume."""
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not Path(transcript).is_absolute():
        raise ValueError("Claude did not provide an absolute transcript path")
    source = Path(transcript)
    atomic_write_json(
        path,
        {
            "transcript": transcript,
            "offset": source.stat().st_size if source.exists() else 0,
            "session": payload.get("session_id"),
            "model": payload.get("model"),
        },
    )
    atomic_write_json(path.with_suffix(".activity.json"), {"idle": True})


def add_session_hook(settings: dict, path: Path) -> None:
    settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {
            "matcher": "startup|resume|clear",
            "hooks": [
                {
                    "type": "command",
                    "command": subprocess_cross_os.shell_command(
                        [sys.executable, "-m", __name__, "session", str(path)]
                    ),
                    "timeout": 10,
                }
            ],
        }
    )
    for event in ("UserPromptSubmit", "Stop"):
        settings["hooks"].setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": subprocess_cross_os.shell_command(
                            [sys.executable, "-m", __name__, "activity", str(path)]
                        ),
                        "timeout": 10,
                    }
                ]
            }
        )


class AutoCommand:
    """Recognize an exact command only when Enter submits it separately."""

    def __init__(self):
        self.line: bytes | None = b""

    def feed(self, data: bytes) -> bool:
        if re.fullmatch(rb"\x1b\[[?0-9;]*[Rcn]", data):
            return False
        if data.startswith(b"\x1b[200~") and data.endswith(b"\x1b[201~"):
            self.feed(data[6:-6])
            return False
        if (b"\r" in data or b"\n" in data) and data not in (b"\r", b"\n"):
            self.line = None
            return False
        trigger = False
        for char in data:
            if char in (10, 13):
                trigger = self.line is not None and self.line.strip().lower() == b"/model auto"
                self.line = b""
            elif char in (3, 21):
                self.line = b""
            elif char in (8, 127) and self.line is not None:
                self.line = self.line[:-1]
            elif 32 <= char < 127 and self.line is not None:
                self.line += bytes([char])
                if len(self.line) > 64:
                    self.line = None
            else:
                self.line = None
        return trigger and data in (b"\r", b"\n")


class ModelCommands:
    """Read complete, new command pairs from the session's native transcript."""

    def __init__(self, session_path: Path):
        self.session_path = session_path
        self.session: dict = {}
        self.offset = 0
        self.command: tuple[str, str] | None = None

    def poll(self) -> list[tuple[str, str]]:
        session = read_json_safe(self.session_path)
        if not session or not isinstance(session.get("transcript"), str):
            return []
        if session != self.session:
            self.session = session
            self.offset = session["offset"]
            self.command = None
        result = []
        try:
            with Path(session["transcript"]).open("rb") as stream:
                stream.seek(self.offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    self.offset = stream.tell()
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if (
                        not isinstance(row, dict)
                        or row.get("type") != "user"
                        or row.get("isSidechain")
                        or row.get("sessionId") != session["session"]
                    ):
                        continue
                    message = row.get("message")
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if not isinstance(content, str):
                        continue
                    if match := _COMMAND.fullmatch(content):
                        self.command = (row.get("uuid", ""), match[1].strip())
                    elif match := _OUTPUT.fullmatch(content):
                        if self.command and row.get("parentUuid") == self.command[0]:
                            result.append((self.command[1], _ANSI.sub("", match[1])))
                        self.command = None
        except FileNotFoundError:
            pass
        return result


def preference_path(auto: dict) -> Path:
    key = hashlib.sha256(auto["storeKey"].encode()).hexdigest()
    return APP_DIR / "claude-auto" / f"{key}.json"


def initial_model(auto: dict, explicit: str | None, *, resume: bool) -> tuple[str | None, bool]:
    rows = auto["models"]
    if resume and explicit is None:
        return None, False
    if explicit:
        row = next((row for row in rows if row["autoModel"] == explicit), None)
        return (row["model"], True) if row else (explicit, False)
    saved = (
        read_json_safe(preference_path(auto)).get("model")
        if auto.get("preferSaved", True)
        else None
    )
    allowed = {row["model"] for row in rows}
    return (saved if saved in allowed else auto["mainModel"]), True


def resolve_model_args(args: list[str], auto: dict) -> list[str]:
    """Resolve explicit Auto aliases without interpreting text after --."""
    targets = {row["autoModel"]: row["model"] for row in auto["models"]}
    result = args.copy()
    for index, arg in enumerate(args):
        if arg == "--":
            break
        if arg in {"--model", "-m"} and index + 1 < len(args):
            result[index + 1] = targets.get(args[index + 1], args[index + 1])
        elif arg.startswith("--model="):
            model = arg.partition("=")[2]
            result[index] = "--model=" + targets.get(model, model)
    return result


class AutoSelection:
    def __init__(
        self, auto: dict, model: str | None, enabled: bool, controls: Path, baseline: dict
    ):
        self.auto = auto
        self.model = model or ""
        self.controls = controls
        self.baseline = baseline
        self.enabled = enabled
        self.pending: ModelSwitch | None = None
        self.choose = False
        self.alias_selected = False
        self.set_enabled(enabled)

    def set_enabled(self, enabled: bool) -> None:
        atomic_write_json(
            self.controls,
            {
                key: self.baseline.get(key, "0") if enabled else "0"
                for key in SMART_ROUTING_ENV_KEYS
            },
        )
        self.enabled = enabled

    def observe(
        self, args: str, output: str, *, accepted: bool = False, saved_model: str | None = None
    ) -> str | None:
        if self.pending is not None:
            if args != self.pending.model:
                return None
            switch, self.pending = self.pending, None
            if output.startswith("Set model to ") and accepted:
                self.model = switch.model
                self.alias_selected = False
                self.set_enabled(True)
                if switch.persist:
                    atomic_write_json(preference_path(self.auto), {"model": switch.model})
                return f"Auto mode: main model {switch.model}, subagents smart routed."
            if not output.startswith("Set model to "):
                self.set_enabled(switch.previous_enabled)
                return (
                    "Auto needs a real main model. Select one with /model auto before continuing."
                    if self.alias_selected
                    else "Main model unchanged."
                )
            self.set_enabled(False)
            return "Claude selected a different model; Smart Router is off. Retry /model auto."
        if output.startswith("Set model to "):
            name = re.split(
                r" (?:and saved as|for this session only|with (?:low|medium|high|xhigh|max) effort)\b",
                output.removeprefix("Set model to ").split("\n", 1)[0],
                maxsplit=1,
            )[0]
            aliases = {row["autoModel"] for row in self.auto["models"]}
            if args in aliases or name in aliases or name == "Auto":
                alias = args if args in aliases else name if name in aliases else self.auto["model"]
                self.model = next(
                    row["model"] for row in self.auto["models"] if row["autoModel"] == alias
                )
                self.alias_selected = True
                self.set_enabled(False)
                self.choose = True
                return None
            self.set_enabled(False)
            self.alias_selected = False
            if "saved as your default" in output:
                self.model = saved_model or ""
                return "Smart Router is off."
            row = next(
                (
                    row
                    for row in self.auto["models"]
                    if args == row["model"] or name == row["label"]
                ),
                None,
            )
            self.model = row["model"] if row else args
            return "Smart Router is off."
        # The old native command validates 'auto' as a model ID. Its completed
        # command still provides a local trigger, including after validation fails.
        if args.lower() == "auto":
            self.choose = True
        return None

    def options(self) -> list[tuple[str, str]]:
        rows = sorted(self.auto["models"], key=lambda row: row["model"] != self.model)
        return [(row["model"], row["label"]) for row in rows]


@dataclass(frozen=True)
class ModelSwitch:
    model: str
    previous_enabled: bool = False
    persist: bool = True


def run(
    argv: list[str],
    *,
    auto: dict,
    start_model: str | None,
    enabled: bool,
    controls: Path,
    baseline: dict,
    session_path: Path,
    prepare_switch: Callable[[str], None],
    switch_accepted: Callable[[], bool],
    restore_selection: Callable[[], None],
    saved_model: Callable[[], str | None],
) -> int:
    # Import only on POSIX; the launch gate retains the native minimum on Windows.
    import pty
    import select

    from ucode.smart_routing.claude_pty import TerminalModeGuard, sync_winsize, valid_model_name

    if not os.isatty(0) or not os.isatty(1):
        raise RuntimeError("Claude 2.1.236 Auto requires an interactive terminal.")
    state = AutoSelection(auto, start_model, enabled, controls, baseline)
    commands = ModelCommands(session_path)
    command_input = AutoCommand()
    metrics.agent_handoff()
    master, slave = pty.openpty()
    try:
        proc = subprocess_cross_os.popen(
            argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    previous_winch = signal.getsignal(signal.SIGWINCH)
    previous_term = signal.getsignal(signal.SIGTERM)

    def resize(*_args: object) -> None:
        sync_winsize(master)
        if proc.poll() is None:
            proc.send_signal(signal.SIGWINCH)

    def terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    last_output = time.monotonic()
    pending_since = 0.0
    notice: str | None = None
    initialized = start_model is not None
    initial_switch: ModelSwitch | None = None

    def switch_model(switch: ModelSwitch) -> None:
        nonlocal pending_since
        if not valid_model_name(switch.model):
            raise RuntimeError("Auto's main model is not a valid Claude model ID.")
        prepare_switch(switch.model)
        state.pending = switch
        state.set_enabled(False)
        pending_since = time.monotonic()
        os.write(master, f"\x15/model {switch.model}".encode())
        # Claude's input paste and submission must be separate terminal events.
        time.sleep(0.3)
        os.write(master, b"\r")

    try:
        with TerminalModeGuard() as terminal:
            signal.signal(signal.SIGWINCH, resize)
            signal.signal(signal.SIGTERM, terminate)
            resize()
            while True:
                events = commands.poll()
                if not initialized and commands.session:
                    selected = commands.session.get("model")
                    row = next(
                        (row for row in auto["models"] if row["autoModel"] == selected), None
                    )
                    state.model = (
                        row["model"] if row else selected if isinstance(selected, str) else ""
                    )
                    state.alias_selected = row is not None
                    state.set_enabled(False)
                    if row is not None:
                        initial_switch = ModelSwitch(row["model"], False, persist=False)
                    initialized = True
                for args, output in events:
                    internal = state.pending is not None and args == state.pending.model
                    notice = state.observe(
                        args,
                        output,
                        accepted=internal and switch_accepted(),
                        saved_model=saved_model(),
                    )
                    if internal:
                        restore_selection()
                if state.pending and time.monotonic() - pending_since > 120:
                    state.pending = None
                    state.set_enabled(False)
                    restore_selection()
                    notice = "Model selection timed out. Retry by selecting Auto in /model."
                if initial_switch is not None and time.monotonic() - last_output >= 0.3:
                    switch_model(initial_switch)
                    initial_switch = None
                if state.choose and time.monotonic() - last_output >= 0.3:
                    state.choose = False
                    terminal.__exit__()
                    os.write(1, b"\x1b[?1049h\x1b[2J\x1b[H")
                    try:
                        chosen = prompt_for_selection(
                            "Auto: choose your main model", state.options(), escape_to_cancel=True
                        )
                    finally:
                        os.write(1, b"\x1b[?1049l")
                        terminal.__enter__()
                        os.write(master, b"\x0c")
                    switch = (
                        ModelSwitch(chosen, state.enabled)
                        if chosen is not None
                        else ModelSwitch(state.model, False, persist=False)
                        if state.alias_selected
                        else None
                    )
                    if switch is not None:
                        switch_model(switch)
                if notice:
                    os.write(1, ("\r\nUG: " + notice + "\r\n").encode())
                    notice = None
                ready, _, _ = select.select([master, 0], [], [], 0.1)
                if master in ready:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    os.write(1, chunk)
                    last_output = time.monotonic()
                if 0 in ready:
                    data = os.read(0, 4096)
                    if not data:
                        break
                    auto_command = command_input.feed(data)
                    idle = (
                        read_json_safe(session_path.with_suffix(".activity.json")).get("idle")
                        is True
                    )
                    if auto_command and idle and state.pending is None:
                        os.write(master, b"\x15")
                        if not state.alias_selected and state.model in {
                            row["model"] for row in auto["models"]
                        }:
                            state.set_enabled(True)
                        state.choose = True
                        continue
                    os.write(master, data)
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        signal.signal(signal.SIGTERM, previous_term)
        os.close(master)
        try:
            restore_selection()
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
    return proc.returncode


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"session", "activity"}:
        raise SystemExit(
            "Usage: python -m ucode.smart_routing.claude_legacy_auto session|activity PATH"
        )
    payload = json.load(sys.stdin)
    path = Path(sys.argv[2])
    if sys.argv[1] == "session":
        session_hook(path, payload)
    else:
        atomic_write_json(
            path.with_suffix(".activity.json"), {"idle": payload.get("hook_event_name") == "Stop"}
        )
