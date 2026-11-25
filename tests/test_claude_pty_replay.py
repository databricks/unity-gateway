"""Focused tests for the PTY paste/submit and fail-open fixes in claude_pty.py.

These tests cover:
- Paste and Enter are separate writes with PASTE_SUBMIT_DELAY_S gap (Bug A fix).
- Enter is retried when no submission signal arrives (Bug B fix).
- Enter stops retrying once the submission signal fires (Bug B fix).
- Switch-timeout path submits the prompt after Claude settles (Bug C fix).
- Persist-timeout path does not SIGTERM and submits (Bug C fix).
- waiting_to_switch timeout submits without /model (Bug D fix).
- Every write into the input box first clears the blocked prompt that Claude Code
  keeps there, so /model runs as a command and the replay is not mangled (Bug E fix).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

if sys.platform != "win32":
    from ucode.smart_routing import claude_pty

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Claude PTY routing is POSIX-only")

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _route_sonnet(_prompt: str) -> claude_pty.FirstPromptRoute:
    return claude_pty.FirstPromptRoute(model="sonnet", display_model="sonnet", rationale="")


def _write_fake_claude(path: Path, body: str) -> None:
    """Write a fake-claude script with the standard prologue."""
    path.write_text(
        """\
import json
import os
import select as sel
import signal
import socket
import sys
import time
import tty
from pathlib import Path

socket_path = sys.argv[1]
capture_path = Path(sys.argv[2])
test_dir = capture_path.parent
signal.alarm(35)

def send_hook(sock_path, prompt):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(sock_path)
    c.sendall((json.dumps({
        "method": "route_first_prompt",
        "prompt": prompt,
        "session_id": "s1",
    }) + "\\n").encode())
    resp = json.loads(c.makefile("rb").readline())
    c.close()
    return resp

def read_until(suffix):
    data = b""
    while not data.endswith(suffix):
        data += os.read(0, 1)
    return data

"""
        + body
    )


@contextmanager
def _stdin_after(ready: Path, chunks: list[bytes], *, after_first: Path | None = None):
    pipe_read, pipe_write = os.pipe()
    saved_stdin = os.dup(0)
    sent = []

    def send():
        deadline = time.monotonic() + 25
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ready.exists():
            for chunk in chunks:
                os.write(pipe_write, chunk)
                sent.append(chunk)
                if len(sent) == 1 and after_first is not None:
                    deadline = time.monotonic() + 25
                    while not after_first.exists() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    if not after_first.exists():
                        return
                time.sleep(0.01)

    writer = threading.Thread(target=send)
    try:
        os.dup2(pipe_read, 0)
        writer.start()
        yield
    finally:
        os.dup2(saved_stdin, 0)
        writer.join(timeout=26)
        os.close(saved_stdin)
        os.close(pipe_read)
        os.close(pipe_write)
    assert not writer.is_alive()
    assert sent == chunks


# ---------------------------------------------------------------------------
# Test A: Paste and Enter are separate writes with the configured delay
# ---------------------------------------------------------------------------


class TestPasteAndEnterSeparate:
    def test_paste_and_enter_have_a_measurable_gap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.12)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")                        # /model command
os.write(1, b"Set model to sonnet\\n")    # confirm switch
sys.stdout.flush()

# Read paste payload (stops at paste-close, no trailing CR)
paste = read_until(b"\\x1b[201~")
t1 = time.monotonic()

# CR arrives separately after PASTE_SUBMIT_DELAY_S
enter = os.read(0, 1)
t2 = time.monotonic()

capture_path.write_text(json.dumps({
    "paste_ends_no_cr": not paste.endswith(b"\\r"),
    "enter_is_cr": enter == b"\\r",
    "gap_s": t2 - t1,
}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
        )

        assert result == 0
        data = json.loads(capture.read_text())
        assert data["paste_ends_no_cr"], "paste payload must not include a trailing CR"
        assert data["enter_is_cr"], "the separate byte must be CR"
        # Gap must be at least PASTE_SUBMIT_DELAY_S minus small scheduling slack.
        assert data["gap_s"] >= 0.10, f"gap {data['gap_s']:.3f}s is less than PASTE_SUBMIT_DELAY_S"


# ---------------------------------------------------------------------------
# Test B: Enter is retried when the submission signal never arrives
# ---------------------------------------------------------------------------


class TestEnterRetry:
    def test_enter_is_retried_when_no_submission_signal(self, tmp_path, monkeypatch):
        """Without a post-claim hook call the loop resends CR up to MAX_SUBMIT_ATTEMPTS."""
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "SUBMIT_VERIFY_S", 0.15)
        monkeypatch.setattr(claude_pty, "MAX_SUBMIT_ATTEMPTS", 3)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")                        # /model command
os.write(1, b"Set model to sonnet\\n")
sys.stdout.flush()

read_until(b"\\x1b[201~")                # paste payload

# Count CR bytes arriving over a generous window; no hook call so retries happen.
cr_count = 0
deadline = time.monotonic() + 2.0
while time.monotonic() < deadline:
    r, _, _ = sel.select([0], [], [], 0.05)
    if r:
        b = os.read(0, 1)
        if b == b"\\r":
            cr_count += 1
            # Exit after collecting MAX_SUBMIT_ATTEMPTS worth of CRs.
            if cr_count >= 3:
                break

capture_path.write_text(json.dumps({"cr_count": cr_count}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
        )

        assert result == 0
        data = json.loads(capture.read_text())
        assert data["cr_count"] == 3, "expected the initial Enter plus two retries"

    def test_enter_stops_retrying_on_submission_signal(self, tmp_path, monkeypatch):
        """A post-claim hook call fires submitted_event and stops further CR retries."""
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "SUBMIT_VERIFY_S", 0.4)  # long window so retries are slow
        monkeypatch.setattr(claude_pty, "MAX_SUBMIT_ATTEMPTS", 5)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")                        # /model command
os.write(1, b"Set model to sonnet\\n")
sys.stdout.flush()

read_until(b"\\x1b[201~")                # paste payload

# Read exactly one CR (the initial Enter).
cr1 = os.read(0, 1)

# Immediately signal submission so no retries happen.
resp2 = send_hook(socket_path, "hello")

# Wait a moment; no more CRs should arrive with SUBMIT_VERIFY_S=0.4.
extra_cr = 0
deadline = time.monotonic() + 0.3
while time.monotonic() < deadline:
    r, _, _ = sel.select([0], [], [], 0.05)
    if r:
        b = os.read(0, 1)
        if b == b"\\r":
            extra_cr += 1

capture_path.write_text(json.dumps({
    "first_cr": cr1 == b"\\r",
    "second_action": resp2.get("action"),
    "extra_cr": extra_cr,
}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
        )

        assert result == 0
        data = json.loads(capture.read_text())
        assert data["first_cr"], "initial Enter must be CR"
        assert data["second_action"] == "allow", "post-claim hook must be allowed"
        assert data["extra_cr"] == 0, "no retry CRs expected after submission signal"


# ---------------------------------------------------------------------------
class TestUnterminatedPaste:
    def test_unterminated_paste_does_not_hide_confirmed_submission(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.01)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.01)
        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        log = tmp_path / "routing.log"
        ready = tmp_path / "ready"
        submit = tmp_path / "submit"
        acknowledged = threading.Event()
        serve = claude_pty.serve_first_prompt_socket
        select = claude_pty.select.select

        def observe_submission(*args, **kwargs):
            callback = kwargs["on_submitted_prompt"]

            def submitted(prompt):
                callback(prompt)
                acknowledged.set()

            kwargs["on_submitted_prompt"] = submitted
            return serve(*args, **kwargs)

        def ready_after_acknowledgement(read, write, error, timeout):
            readable = select(read, write, error, timeout)
            if 0 in readable[0] and ready.exists() and not acknowledged.is_set():
                # Put the real hook acknowledgement and the open paste in the same loop turn.
                submit.touch()
                assert acknowledged.wait(3)
            return readable

        monkeypatch.setattr(claude_pty, "serve_first_prompt_socket", observe_submission)
        monkeypatch.setattr(claude_pty.select, "select", ready_after_acknowledgement)
        _write_fake_claude(
            fake_claude,
            """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
os.write(1, b"Set model to sonnet\\n")
read_until(b"\\x1b[201~")
assert os.read(0, 1) == b"\\r"
(test_dir / "ready").touch()
deadline = time.monotonic() + 3
while not (test_dir / "submit").exists():
    assert time.monotonic() < deadline
    time.sleep(0.005)
assert send_hook(socket_path, "hello")["action"] == "allow"
received = read_until(b"\\x1b[200~")
deadline = time.monotonic() + 0.3
while time.monotonic() < deadline:
    if sel.select([0], [], [], 0.01)[0]:
        received += os.read(0, 4096)
capture_path.write_text(json.dumps({"received": received.hex()}))
""",
        )
        with _stdin_after(ready, [b"\x1b[200~"]):
            result = claude_pty.run_claude_pty(
                [sys.executable, str(fake_claude), str(socket_path), str(capture)],
                route_prompt=_route_sonnet,
                socket_path=socket_path,
                log_path=log,
            )

        assert result == 0
        assert json.loads(capture.read_text())["received"] == b"\x1b[200~".hex()
        assert "[DONE] first prompt confirmed submitted" in log.read_text()
        assert "[RETRY]" not in log.read_text()


def test_prompt_recovery_escapes_control_bytes_when_the_file_cannot_be_saved(tmp_path, monkeypatch):
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied")
    monkeypatch.setattr(claude_pty, "APP_DIR", unavailable)
    prompt = 'Keep this "quoted" prompt.\n\x1b[2J\x7f'

    note = claude_pty.prompt_recovery_note(prompt)

    assert json.loads(note.split("JSON string: ", 1)[1]) == prompt
    assert "\x1b" not in note and "\x7f" not in note and "\n" not in note


# Test C: Switch-timeout path submits the prompt (fail-open, Bug C)
# ---------------------------------------------------------------------------


@pytest.fixture
def replay_model_switch(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "haiku", "unrelated": "preserved"}))
    wrapper = """
import json
import sys
import time
from pathlib import Path
from ucode.smart_routing import claude_pty, v2

root = Path(sys.argv[1])
settings = root / ".claude" / "settings.json"
guard = v2._ClaudeModelSettingGuard(settings)
observations = {"restorations": [], "checked_at": [], "deferred": 0, "errors": []}
# Fake TUIs synchronize on phase markers; human-input deadlines only slow these checks.
claude_pty.SWITCH_TIMEOUT_S = 1.0
claude_pty.READY_TIMEOUT_S = 2.0

def persisted():
    observations["checked_at"].append(time.monotonic())
    result = guard.is_routed()
    (root / "persistence-checked").touch()
    return result

def restore():
    observations["restorations"].append(guard.is_routed())
    try:
        restored = guard.restore()
    except (OSError, ValueError, RuntimeError) as exc:
        observations["errors"].append(type(exc).__name__)
        (root / "restore-failed").touch()
        raise
    if restored:
        (root / "restored").touch()
    return restored

def defer():
    guard.defer()
    observations["deferred"] += 1
    (root / "deferred").touch()

def abandon(manual_model):
    try:
        return guard.abandon(manual_model)
    except OSError:
        (root / "handoff-failed").touch()
        raise

try:
    result = claude_pty.run_claude_pty(
        [sys.executable, str(root / "fc.py"), str(root / "first.sock"),
         str(root / "capture.json"), str(settings)],
        route_prompt=lambda _: claude_pty.FirstPromptRoute(
            model="sonnet", display_model="sonnet", rationale=""),
        socket_path=root / "first.sock",
        prepare_model_switch=guard.begin,
        model_switch_persisted=persisted,
        restore_model_setting=restore,
        defer_model_setting=defer,
        abandon_model_setting=abandon,
        confirm_manual_model_setting=guard.confirm_manual_choice,
        cancel_manual_model_setting=guard.cancel_manual_choice,
        prepare_saved_model_setting=guard.prepare_saved_model_choice,
        confirm_saved_model_setting=guard.confirm_saved_model_choice,
        log_path=root / "routing.log",
    )
finally:
    observations["finished"] = guard.finish()
    (root / "wrapper.json").write_text(json.dumps(observations))
sys.exit(result)
"""

    def run(body, *, wrapper_setup=""):
        _write_fake_claude(tmp_path / "fc.py", body)
        configured_wrapper = wrapper.replace(
            "try:\n    result = claude_pty.run_claude_pty(",
            wrapper_setup + "\ntry:\n    result = claude_pty.run_claude_pty(",
        )
        result = subprocess.run(
            [sys.executable, "-c", configured_wrapper, str(tmp_path)],
            stdin=0,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
            timeout=45,
        )
        assert result.returncode == 0
        observations = json.loads((tmp_path / "wrapper.json").read_text())
        assert observations["finished"]
        return (
            json.loads((tmp_path / "capture.json").read_text()),
            json.loads(settings.read_text()),
            observations,
        )

    return run


class TestSwitchTimeoutFailOpen:
    def test_confirmed_cancellation_replays_on_the_original_model(self, replay_model_switch):
        capture, settings, observations = replay_model_switch(
            """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
os.write(1, b"Kept model as haiku\\n")
paste = read_until(b"\\x1b[201~")
assert os.read(0, 1) == b"\\r"
prompt = paste.split(b"\\x1b[200~", 1)[1].split(b"\\x1b[201~", 1)[0].decode()
assert send_hook(socket_path, prompt)["action"] == "allow"
capture_path.write_text(json.dumps({
    "prompt": prompt, "settings_while_running": json.loads(Path(sys.argv[3]).read_text()),
}))
"""
        )

        assert capture["prompt"] == "hello"
        assert observations["restorations"] == [False]
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        ("persist_delay", "intermediate_redraw"),
        [(0.0, False), (0.0, True), (0.8, False), (2.4, False)],
        ids=["persisted-silent", "persisted-redraw", "pending-write", "late-write"],
    )
    def test_late_model_completion_cannot_discard_the_replayed_prompt(
        self, replay_model_switch, persist_delay, intermediate_redraw
    ):
        capture, settings, observations = replay_model_switch(
            f"PERSIST_DELAY = {persist_delay}\nINTERMEDIATE_REDRAW = {intermediate_redraw}\n"
            + """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")

# A late /model completion clears any prompt replayed before its final redraw.
ready_at = None
model_complete = False
persist_at = None
received = b""
submitted = []
deadline = time.monotonic() + 25
while time.monotonic() < deadline:
    if ready_at is not None and not model_complete and time.monotonic() >= ready_at:
        model_complete = True
        persist_at = time.monotonic() + PERSIST_DELAY
        os.write(1, b"Set model to sonnet\\n")
    if persist_at is not None and time.monotonic() >= persist_at:
        settings = Path(sys.argv[3])
        saved = json.loads(settings.read_text())
        saved["model"] = "sonnet"
        settings.write_text(json.dumps(saved))
        persist_at = None
    if not sel.select([0], [], [], 0.01)[0]:
        continue
    data = os.read(0, 4096)
    if not model_complete:
        if ready_at is None and b"\\x1b" in data:
            ready_at = time.monotonic() + 0.3
            if INTERMEDIATE_REDRAW:
                os.write(1, b"Finishing model command...\\n")
        continue
    received += data
    if b"\\x1b[200~" in received and b"\\x1b[201~" in received:
        _, rest = received.split(b"\\x1b[200~", 1)
        prompt, rest = rest.split(b"\\x1b[201~", 1)
        if b"\\r" in rest:
            submitted.append(prompt.decode())
            assert send_hook(socket_path, prompt.decode())["action"] == "allow"
            received = b""
            deadline = min(deadline, time.monotonic() + 0.8)
capture_path.write_text(json.dumps({
    "submitted": submitted, "settings_while_running": json.loads(Path(sys.argv[3]).read_text()),
}))
""",
        )

        assert capture["submitted"] == ["hello"]
        assert observations["restorations"] == [True]
        assert (
            settings
            == capture["settings_while_running"]
            == {
                "model": "haiku",
                "unrelated": "preserved",
            }
        )
        assert bool(observations["deferred"]) == (persist_delay > 2.0)

    @pytest.mark.parametrize(
        ("chunks", "replay"),
        [
            ([b"\x1b[", b"A"], True),
            ([b"\x1b[", b"?1;2c"], True),
            ([b"\x1b[", b"Anew input"], False),
            ([b"\x1b["], False),
            ([b"new input"], False),
            ([b"explain codx\x1b[D\x7fe\x1b[C\r"], False),
            ([b"\x1b[200~", b"user paste\x1b[201~"], False),
        ],
        ids=[
            "arrow",
            "terminal-reply",
            "edited-after-reply",
            "unfinished-reply",
            "user-edit",
            "cursor-edit",
            "paste",
        ],
    )
    def test_input_during_model_recovery(
        self, replay_model_switch, tmp_path, capfd, chunks, replay
    ):
        body = (
            f"INPUT = {chunks[0]!r}\n"
            + """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
settings = Path(sys.argv[3])
os.write(1, b"Set model to sonnet\\n")
while not (test_dir / "persistence-checked").exists():
    time.sleep(0.005)
(test_dir / "ready").touch()
received = b""
while len(received) < len(INPUT):
    received += os.read(0, len(INPUT) - len(received))
assert received == INPUT
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
received = b""
submitted = []
forwarded = b""
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    if sel.select([0], [], [], 0.01)[0]:
        chunk = os.read(0, 4096)
        received += chunk
        forwarded += chunk
    if b"\\x1b[200~" in received and b"\\x1b[201~\\r" in received:
        prompt = received.split(b"\\x1b[200~", 1)[1].split(b"\\x1b[201~", 1)[0].decode()
        submitted.append(prompt)
        assert send_hook(socket_path, prompt)["action"] == "allow"
        received = b""
capture_path.write_text(json.dumps({
    "submitted": submitted, "forwarded": forwarded.hex(),
    "settings_while_running": json.loads(settings.read_text()),
}))
"""
        )
        restored = tmp_path / "restored"
        with _stdin_after(tmp_path / "ready", chunks, after_first=restored):
            capture, settings, _ = replay_model_switch(body)

        assert restored.exists()
        assert capture["submitted"] == (["hello"] if replay else [])
        if not replay:
            assert capture["forwarded"] == b"".join(chunks[1:]).hex()
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )
        assert ("Resend your prompt" in capfd.readouterr().out) == (not replay)

    @pytest.mark.parametrize(
        "command",
        [b"/help\r", b"explain codx\x1b[D\x7fe\x1b[C\r"],
    )
    def test_unrelated_command_preserves_another_sessions_recovery(
        self, replay_model_switch, tmp_path, command
    ):
        body = (
            f"COMMAND = {command!r}\n"
            + """\
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
os.write(1, b"Set model to sonnet\\n")
paste = read_until(b"\\x1b[201~")
assert b"\\x1b[200~hello\\x1b[201~" in paste
assert os.read(0, 1) == b"\\r"
assert send_hook(socket_path, "hello")["action"] == "allow"
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert (test_dir / "restored").exists()
assert json.loads(settings.read_text())["model"] == "haiku"
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("opus")
    (test_dir / "ready").touch()
    command = read_until(b"\\r")
    assert command == COMMAND
    settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
    assert other.is_routed()
    assert other.restore()
    capture_path.write_text(json.dumps({
        "command": command.hex(), "settings_while_running": json.loads(settings.read_text()),
    }))
finally:
    other.finish()
"""
        )
        with _stdin_after(tmp_path / "ready", [command]):
            capture, settings, _ = replay_model_switch(body)

        assert capture["command"] == command.hex()
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        ("manual_model", "completion"),
        [
            ("opus", b"Kept model as sonnet\n"),
            pytest.param(
                "opus",
                b"Model switch to {model} was blocked by a PreModelSwitch hook: policy\n",
                id="opus-premodel-rejection",
            ),
            pytest.param(
                "sonnet",
                b"Model switch to {model} was blocked by a PreModelSwitch hook: policy\n",
                id="sonnet-premodel-rejection",
            ),
        ],
    )
    def test_cancelled_manual_switch_releases_another_sessions_recovery(
        self, replay_model_switch, tmp_path, completion, manual_model
    ):
        body = (
            f"COMPLETION = {completion.replace(b'{model}', manual_model.encode())!r}\n"
            f"MANUAL_MODEL = {manual_model!r}\n"
            + """\
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
os.write(1, b"Set model to sonnet\\n")
read_until(b"\\x1b[201~")
assert os.read(0, 1) == b"\\r"
assert send_hook(socket_path, "hello")["action"] == "allow"
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert (test_dir / "restored").exists()
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("sonnet")
    (test_dir / "ready").touch()
    assert read_until(b"\\r") == f"/model {MANUAL_MODEL}\\r".encode()
    settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
    deadline = time.monotonic() + 3
    while not other.is_routed():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert other.restore() is False
    os.write(1, COMPLETION)
    deadline = time.monotonic() + 3
    while not other.restore() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(settings.read_text())["model"] == "haiku"
    assert other.begin("fable")
    assert other.restore()
    capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
finally:
    other.finish()
"""
        )
        with _stdin_after(tmp_path / "ready", [f"/model {manual_model}\r".encode()]):
            capture, settings, _ = replay_model_switch(body)

        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        ("chunks", "fail_handoff", "after_submission"),
        [
            ([b"/model ", b"sonnet\r"], False, False),
            ([b"\x1b[A", b"\r"], False, False),
            pytest.param(
                [b"/model sonnet\r"],
                True,
                False,
                marks=pytest.mark.skipif(
                    os.name != "posix" or os.geteuid() == 0,
                    reason="requires unprivileged POSIX directory permissions",
                ),
            ),
            ([b"/model sonnet\r"], False, True),
        ],
        ids=["typed", "history", "handoff-error", "submitted"],
    )
    def test_manual_selection_of_the_routed_model_survives_recovery(
        self, replay_model_switch, tmp_path, chunks, fail_handoff, after_submission
    ):
        body = (
            f"FAIL_HANDOFF = {fail_handoff}\nAFTER_SUBMISSION = {after_submission}\n"
            + """\
import subprocess
from ucode import agents

helper = Path(agents.__file__).parent / "claude_mods" / "model-setting-receipt.py"
def receipt(payload):
    response = subprocess.run(
        [sys.executable, str(helper), socket_path], input=json.dumps(payload),
        text=True, capture_output=True, timeout=10,
    )
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["acknowledged"]

assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
receipt({"event": "command", "id": 1, "command": "model", "origin": "composer"})
if AFTER_SUBMISSION:
    os.write(1, b"Set model to sonnet\\n")
    paste = read_until(b"\\x1b[201~")
    assert b"\\x1b[200~hello\\x1b[201~" in paste
    assert os.read(0, 1) == b"\\r"
    assert send_hook(socket_path, "hello")["action"] == "allow"
else:
    assert os.read(0, 1) == b"\\x1b"
    while not (test_dir / "deferred").exists():
        time.sleep(0.01)
recovery_dir = Path.home() / ".ucode"
try:
    if FAIL_HANDOFF:
        recovery_dir.chmod(0o500)
        assert not os.access(recovery_dir, os.W_OK), "requires unprivileged POSIX permissions"
    (test_dir / "ready").touch()
    command = read_until(b"\\r")
    if FAIL_HANDOFF:
        assert (test_dir / "handoff-failed").exists()
finally:
    recovery_dir.chmod(0o700)
receipt({"event": "command", "id": 2, "command": "model", "origin": "composer"})
receipt({"event": "prepare", "id": 3, "command_id": 2})
Path(sys.argv[3]).write_text(json.dumps({"model": "sonnet"}))
receipt({"event": "saved", "id": 3, "model": "sonnet"})
os.write(1, b"Set model to sonnet\\n")
time.sleep(0.8)
capture_path.write_text(json.dumps({
    "command": command.hex(), "settings_while_running": json.loads(Path(sys.argv[3]).read_text()),
}))
"""
        )
        with _stdin_after(tmp_path / "ready", chunks):
            capture, settings, _ = replay_model_switch(body)
        assert capture["command"] == b"".join(chunks).hex()
        assert capture["settings_while_running"] == settings == {"model": "sonnet"}
        assert (tmp_path / "handoff-failed").exists() is fail_handoff

    @pytest.mark.parametrize(
        "chunks", [[b"/model haiku\r"], [b"/mo", b"del hai", b"ku\r"]], ids=["typed", "fragmented"]
    )
    def test_manual_model_command_protects_against_another_sessions_late_write(
        self, replay_model_switch, tmp_path, chunks
    ):
        body = """\
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("sonnet")
    other.defer()
    time.sleep(v2.CLAUDE_MODEL_DEFER_TIMEOUT_S + 0.1)
    assert not other.is_routed()
    assert send_hook(socket_path, "hello")["action"] == "block"
    tty.setraw(0)
    print("blocked", flush=True)
    read_until(b"\\r")
    assert os.read(0, 1) == b"\\x1b"
    while not (test_dir / "deferred").exists():
        time.sleep(0.01)
    (test_dir / "ready").touch()
    command = read_until(b"\\r")
    # Keep polling from observing the manual setting before the competing write.
    with (v2.APP_DIR / "claude-v2-model.lock").open("a+") as lock:
        v2.acquire_exclusive_file_lock(lock, timeout=2)
        try:
            settings.write_text(json.dumps({"model": "haiku", "unrelated": "preserved"}))
            settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
        finally:
            v2.release_file_lock(lock)
    os.write(1, b"SetmodeltoClaudeHaiku5.5andsavedasyourdefaultfornewsessions\\r\\r\\n")
    deadline = time.monotonic() + 2
    # Native confirmation may briefly hold the nonblocking recovery lock.
    while not other.is_routed():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    while not other.restore():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    capture_path.write_text(json.dumps({
        "command": command.hex(), "settings_while_running": json.loads(settings.read_text()),
    }))
finally:
    other.finish()
"""
        with _stdin_after(tmp_path / "ready", chunks):
            capture, settings, _ = replay_model_switch(body)
        assert capture["command"] == b"".join(chunks).hex()
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.skipif(
        os.name != "posix" or os.geteuid() == 0,
        reason="requires unprivileged POSIX directory permissions",
    )
    def test_restore_write_failure_is_retried_while_claude_runs(self, replay_model_switch):
        capture, settings, observations = replay_model_switch(
            """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
settings = Path(sys.argv[3])
saved = json.loads(settings.read_text())
saved["model"] = "sonnet"
settings.write_text(json.dumps(saved))
try:
    settings.parent.chmod(0o500)
    assert not os.access(settings.parent, os.W_OK), "requires unprivileged POSIX permissions"
    os.write(1, b"Set model to sonnet\\n")
    while not (test_dir / "restore-failed").exists():
        time.sleep(0.005)
    assert json.loads(settings.read_text()) == saved
finally:
    settings.parent.chmod(0o700)
paste = read_until(b"\\x1b[201~")
assert os.read(0, 1) == b"\\r"
prompt = paste.split(b"\\x1b[200~", 1)[1].split(b"\\x1b[201~", 1)[0].decode()
assert send_hook(socket_path, prompt)["action"] == "allow"
capture_path.write_text(json.dumps({
    "prompt": prompt, "settings_while_running": json.loads(settings.read_text()),
}))
"""
        )

        assert capture["prompt"] == "hello"
        assert observations["restorations"] == [True, True]
        assert observations["errors"] == ["PermissionError"]
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    def test_deferred_recovery_backs_off_and_restores_a_late_write(self, replay_model_switch):
        capture, settings, observations = replay_model_switch(
            """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
while not (test_dir / "deferred").exists():
    time.sleep(0.01)
deferred_at = time.monotonic()
received = b""
while time.monotonic() - deferred_at < 6:
    os.write(1, b"waiting\\r")
    if sel.select([0], [], [], 0.05)[0]:
        received += os.read(0, 4096)
settings = Path(sys.argv[3])
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
write_at = time.monotonic()
while not (test_dir / "restored").exists():
    assert time.monotonic() - write_at < 3, "late persistence must still be restored"
    if sel.select([0], [], [], 0.05)[0]:
        received += os.read(0, 4096)
capture_path.write_text(json.dumps({
    "received": received.hex(), "deferred_at": deferred_at, "write_at": write_at,
    "settings_while_running": json.loads(settings.read_text()),
}))
"""
        )

        checks = [
            instant
            for instant in observations["checked_at"]
            if capture["deferred_at"] <= instant < capture["write_at"]
        ]
        assert 3 <= len(checks) <= 9, "deferred recovery must back off during a long session"
        assert (
            max(later - earlier for earlier, later in zip(checks, checks[1:], strict=False)) >= 0.75
        )
        assert capture["received"] == "", "late persistence must not restart automatic input"
        assert observations["restorations"] == [True]
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    def test_missing_late_write_does_not_reserve_the_default_until_exit(self, replay_model_switch):
        capture, settings, _ = replay_model_switch(
            """\
from ucode.smart_routing import v2

assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
while not (test_dir / "deferred").exists():
    time.sleep(0.01)
settings = Path(sys.argv[3])
other = v2._ClaudeModelSettingGuard(settings)
first_attempt = prepared = other.begin("opus")
started = time.monotonic()
while not prepared and time.monotonic() - started < 5:
    time.sleep(0.02)
    prepared = other.begin("opus")
try:
    assert prepared
    settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
    assert other.restore()
finally:
    other.finish()
capture_path.write_text(json.dumps({
    "first_attempt": first_attempt, "prepared": prepared,
    "settings_while_running": json.loads(settings.read_text()),
}))
"""
        )

        assert capture["first_attempt"] is False
        assert capture["prepared"] is True
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        ("output", "later_model"),
        [("silent", "user-selected"), ("busy", None), ("cancelled", "sonnet")],
    )
    def test_unconfirmed_switch_stops_automatic_input(
        self, replay_model_switch, tmp_path, capfd, output, later_model
    ):
        original = (
            "Explain this code.\n"
            + "A long prompt with caf\u00e9 and terminal \x1b[2J bytes. " * 50
        )
        capture, settings, observations = replay_model_switch(
            f"OUTPUT = {output!r}\nLATER_MODEL = {later_model!r}\nORIGINAL = {original!r}\n"
            + """\
import fcntl

assert send_hook(socket_path, ORIGINAL)["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
if OUTPUT == "cancelled":
    os.write(1, b"Cancelled model switch\\n")
received = b""
while not (test_dir / "deferred").exists():
    if OUTPUT == "busy":
        os.write(1, b"busy\\r")
    if sel.select([0], [], [], 0.01)[0]:
        received += os.read(0, 4096)

with (Path.home() / ".ucode" / "claude-v2-model.lock").open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
settings = Path(sys.argv[3])
if LATER_MODEL is not None:
    saved = json.loads(settings.read_text())
    saved["model"] = LATER_MODEL
    settings.write_text(json.dumps(saved))
    if LATER_MODEL == "sonnet":
        os.write(1, b"Set model to sonnet\\n")
deadline = time.monotonic() + 1
while time.monotonic() < deadline:
    if OUTPUT == "busy":
        os.write(1, b"busy\\r")
    if sel.select([0], [], [], 0.01)[0]:
        received += os.read(0, 4096)
capture_path.write_text(json.dumps({
    "received": received.hex(), "settings_while_running": json.loads(settings.read_text()),
}))
""",
        )

        assert capture["received"] == "", "a redraw must not trigger automatic input"
        assert observations["deferred"] == 1
        assert observations["restorations"] == ([True] if later_model == "sonnet" else [])
        checks = observations["checked_at"]
        assert checks
        if output == "busy":
            assert len(checks) >= 2
        assert all(
            later - earlier >= claude_pty.MODEL_RESTORE_INTERVAL_S - 0.01
            for earlier, later in zip(checks, checks[1:], strict=False)
        ), "busy output must not cause a settings read on every chunk"
        assert (
            capture["settings_while_running"]
            == settings
            == {
                "model": "user-selected" if later_model == "user-selected" else "haiku",
                "unrelated": "preserved",
            }
        )
        assert "automatic replay stopped" in (tmp_path / "routing.log").read_text()
        copies = list((tmp_path / ".ucode").glob("claude-prompt-*.txt"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == original.encode()
        assert copies[0].stat().st_mode & 0o777 == 0o600
        terminal = capfd.readouterr().out
        assert str(copies[0]) in terminal
        assert "\x1b[2J" not in terminal


class TestManualModelConfirmation:
    @pytest.mark.parametrize(
        "model, output, expected",
        [
            ("opus", b"Model switch to opus was blocked by a PreModelSwitch hook\n", "cancelled"),
            (
                "opus",
                b"Model switch to Claude Opus 5.5 was blocked by a PreModelSwitch hook: policy\n",
                "cancelled",
            ),
            (
                None,
                b"Model switch to Custom Display was blocked by a PreModelSwitch hook\n",
                "cancelled",
            ),
            ("opus", b"Model switch to sonnet was blocked by a PreModelSwitch hook\n", None),
            ("opus", b"Model switch to opus was blocked by a PreModelSwitch hook", None),
            (
                None,
                b"Please explain: Model switch to opus was blocked by a PreModelSwitch hook\n",
                None,
            ),
            (None, "❯ Model switch to opus was blocked by a PreModelSwitch hook\n".encode(), None),
        ],
    )
    def test_premodel_rejection_requires_a_complete_matching_native_result(
        self, model, output, expected
    ):
        confirmation = claude_pty.ManualModelConfirmation(model)
        observations = [confirmation.observe(bytes([byte])) for byte in output]
        assert next((result for result in observations if result is not None), None) == expected

    @pytest.mark.parametrize(
        "model, output, expected",
        [
            (
                "haiku",
                b"SetmodeltoClaudeHaiku5.5andsavedasyourdefaultfornewsessions\r\r\n",
                "confirmed",
            ),
            ("haiku", b"Set model to haiku\n", "confirmed"),
            ("haiku", b"SetmodeltoSonnet5.5andsavedasyourdefaultfornewsessions\r\n", None),
            ("haiku", b"SetmodeltoClaudeHaiku5.5", None),
            ("haiku", b"Set model to haiku-other\n", None),
            ("not-a-model", b"Could not set model to not-a-model\n", None),
            ("system.ai.custom", b"Model set to system.ai.custom\n", "confirmed"),
            ("system.ai.custom", b"Model set to Custom Display\n", None),
            ("opus", b"Kept model as sonnet\n", "cancelled"),
            ("sonnet", b"Kept model as sonnet\n", "cancelled"),
            ("opus", b"Model switch cancelled\n", "cancelled"),
        ],
    )
    def test_requires_a_complete_matching_native_result(self, model, output, expected):
        confirmation = claude_pty.ManualModelConfirmation(model)
        observations = [confirmation.observe(bytes([byte])) for byte in output]
        assert next((result for result in observations if result is not None), None) == expected


class TestModelSwitchInput:
    @pytest.mark.parametrize(
        "sequence",
        [
            b"\x1b[A",
            b"\x1bOD",
            b"\x1b[1~",
            b"\x1b[4~",
            b"\x1b[I",
            b"\x1b[O",
            b"\x1b[?1;2c",
            b"\x1b[12;45R",
            b"\x1b[?31u",
            b"\x1b[?2026;1$y",
            b"\x1b[4;2$y",
            b"\x1bP>|XTerm(406)\x1b\\",
            b"\x1bP1$r0m\x1b\\",
            b"\x1bP1+r544e=787465726d\x1b\\",
        ],
    )
    def test_fragmented_navigation_and_terminal_replies_are_not_edits(self, sequence):
        state = claude_pty.ModelSwitchInput()
        for byte in sequence:
            assert state.observe(bytes([byte]), 1.0) == claude_pty.ModelSwitchInputResult(
                edited=False, model_command=False
            )
        assert state.observe(b"", 2.0) == claude_pty.ModelSwitchInputResult(
            edited=False, model_command=False
        )

    def test_escape_alone_cancels_replay_without_relinquishing_the_default(self):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(b"\x1b", 1.0) == claude_pty.ModelSwitchInputResult(
            edited=False, model_command=False
        )
        assert state.observe(b"", 2.0) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )

    @pytest.mark.parametrize(
        "sequence",
        [b"\x1b[97u", b"\x1b[97;1u", b"\x1b[97;5u", b"\x1b[13;1:1u", b"\x1bPcustom\x1b\\"],
    )
    def test_non_reply_sequences_remain_user_input(self, sequence):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(sequence, 1.0).edited

    @pytest.mark.parametrize(
        "line",
        [
            b"explain codx\x1b[D\x7fe\x1b[C\r",
            b"explain code\x1b[4Dx\r",
            b"explain code\x1b[Hmore \x1b[F\r",
            b"explain codx\x1b[D\x1b[3~e\r",
        ],
    )
    def test_cursor_edits_keep_an_ordinary_prompt_known(self, line):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(line, 1.0) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )

    def test_grapheme_cursor_motion_does_not_guess_a_manual_target(self):
        state = claude_pty.ModelSwitchInput()
        result = state.observe("/model opuX\u0301\x1b[D\x1b[3~s\r".encode(), 1.0)
        assert result.manual_model is None

    def test_submitted_slash_command_transfers_default_ownership(self):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(b"/model ", 1.0) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )
        assert state.observe(b"sonnet\r", 1.1) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=True, manual_model="sonnet"
        )
        for command in (b"/mod\r", b"/settings\r"):
            assert state.observe(command, 1.2) == claude_pty.ModelSwitchInputResult(
                edited=True, model_command=True
            )
        assert state.observe(b"plain text\r", 1.3) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )

    @pytest.mark.parametrize(
        "editing",
        [b"\x1b[A", b"\x1bOD", b"\t", b"\x12", b"\x10", b"\x19"],
        ids=["history", "cursor", "completion", "search", "previous", "yank"],
    )
    def test_submitting_an_unknown_line_transfers_default_ownership(self, editing):
        state = claude_pty.ModelSwitchInput()
        state.observe(editing, 1.0)
        assert state.observe(b"\r", 1.1) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=True
        )
        assert state.observe(b"plain text\r", 1.2) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )

    @pytest.mark.parametrize(
        "command, expected",
        [
            (b"  /model system.ai.claude-sonnet-5[1m]\r", "system.ai.claude-sonnet-5[1m]"),
            (b"\x1b[200~/model haiku\x1b[201~\r", "haiku"),
            (b"/model haikx\x7fu\r", "haiku"),
            (b"/model\r", None),
            (b"/model haiku extra\r", None),
            (b"/model haiku\x1b[D\r", "haiku"),
            (b"/model haikx\x1b[D\x1b[3~u\r", "haiku"),
            (b"/model haiku\r\x1b[A\r", None),
            (b"x" * 4096 + b"/model haiku\r", None),
        ],
    )
    def test_manual_target_requires_a_complete_known_command(self, command, expected):
        state = claude_pty.ModelSwitchInput()
        for index, byte in enumerate(command):
            result = state.observe(bytes([byte]), index / 1000)
        assert result.manual_model == expected

    def test_terminal_replies_do_not_make_a_known_line_unknown(self):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(b"plain text", 1.0) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )
        for reply in [b"\x1b[?1;2c", b"\x1b[12;45R", b"\x1b[I", b"\x1b]10;rgb:ff/ff/ff\x07"]:
            assert state.observe(reply, 1.1) == claude_pty.ModelSwitchInputResult(
                edited=False, model_command=False
            )
        assert state.observe(b"\r", 1.2) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )

    def test_multiline_paste_is_not_a_submitted_model_command(self):
        state = claude_pty.ModelSwitchInput()
        assert state.observe(
            b"\x1b[200~/model sonnet\nexplain it\x1b[201~", 1.0
        ) == claude_pty.ModelSwitchInputResult(edited=True, model_command=False)
        assert state.observe(b"\r", 1.1) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )
        assert state.observe(b"\x1b[200~", 1.2) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )
        assert state.pending
        assert state.observe(
            b"/model sonnet\nexplain it\x1b[201~", 1.3
        ) == claude_pty.ModelSwitchInputResult(edited=True, model_command=False)
        assert not state.pending
        assert state.observe(b"\r", 1.4) == claude_pty.ModelSwitchInputResult(
            edited=True, model_command=False
        )


# ---------------------------------------------------------------------------
# Test E: waiting_to_switch timeout submits without /model (Bug D)
# ---------------------------------------------------------------------------


class TestWaitingToSwitchTimeout:
    @pytest.mark.parametrize("contended", [False, True], ids=["busy-output", "model-lock"])
    def test_ready_timeout_submits_without_model_switch(
        self, tmp_path, monkeypatch, capfdbinary, contended
    ):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05 if contended else 100.0)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.3)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

# Read all bytes until paste-open, recording them as pre-paste content.
pre = b""
while True:
    b = os.read(0, 1)
    pre += b
    if pre.endswith(b"\\x1b[200~"):
        pre = pre[:-6]  # strip the paste-open itself
        break

# Read paste payload and Enter.
prompt_bytes = b""
while not prompt_bytes.endswith(b"\\x1b[201~"):
    prompt_bytes += os.read(0, 1)
enter = os.read(0, 1)
assert send_hook(socket_path, "hello")["action"] == "allow"
extra = os.read(0, 4096) if sel.select([0], [], [], 0.2)[0] else b""

capture_path.write_text(json.dumps({
    "extra_input": extra.hex(),
    "no_model_cmd_before_paste": b"/model" not in pre,
    "paste_received": True,
    "enter_is_cr": enter == b"\\r",
}))
""",
        )

        preparations = []

        def prepare(model):
            preparations.append(model)
            return False

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
            prepare_model_switch=prepare,
        )

        assert result == 0
        data = json.loads(capture.read_text())
        assert data["no_model_cmd_before_paste"], (
            "no /model command should be sent when the quiet window times out"
        )
        assert data["paste_received"]
        assert data["enter_is_cr"]

        assert data["extra_input"] == ""
        assert bool(preparations) is contended
        assert b"could not prepare the model switch" in capfdbinary.readouterr().out


# ---------------------------------------------------------------------------
# Test E: the blocked prompt left in Claude Code's input box is cleared (Bug E)
# ---------------------------------------------------------------------------

# A fake TUI with Claude Code 2.1.296's input-box behaviour: a hook-blocked prompt
# stays in the box, Ctrl-U deletes back to the start of the current line (or the
# line break before it), and CR submits the box contents. ESC does not clear the
# box, and ESC while a submitted prompt's UserPromptSubmit hook runs puts that
# prompt back in the box.
_INPUT_BOX_TUI = """\
blocked_prompt = "fix the parser\\nthen run the tests"
resp = send_hook(socket_path, blocked_prompt)
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

box = blocked_prompt
submitted = []
pasting = False
pending = b""
deadline = time.monotonic() + 3.0
while len(submitted) < EXPECTED_SUBMITS and time.monotonic() < deadline:
    if not sel.select([0], [], [], 0.05)[0]:
        if CANCEL_SWITCH and pending == b"\\x1b":
            box, pending = blocked_prompt, b""
            os.write(1, b"Kept model as haiku\\n")
        continue
    pending += os.read(0, 1)
    if pending in (b"\\x1b", b"\\x1b[", b"\\x1b[2", b"\\x1b[20", b"\\x1b[200", b"\\x1b[201"):
        continue
    if pending == b"\\x1b[200~":
        pasting, pending = True, b""
        continue
    if pending == b"\\x1b[201~":
        pasting, pending = False, b""
        continue
    if pending.startswith(b"\\x1b"):
        pending = b""
        continue
    char, pending = pending.decode(), b""
    if char == "\\r" and not pasting:
        submitted.append(box)
        if box.startswith("/model ") and CONFIRM_SWITCH:
            os.write(1, b"Set model to sonnet\\n")
        box = ""
    elif char == "\\x15":
        if box.endswith("\\n"):
            box = box[:-1]
        else:
            box = box[: box.rfind("\\n") + 1]
    else:
        box += char

capture_path.write_text(json.dumps({"submitted": submitted}))
"""


def _run_input_box_tui(
    tmp_path: Path, *, confirm_switch: bool, expected_submits: int, cancel_switch: bool = False
) -> list:
    socket_path = tmp_path / "first.sock"
    capture = tmp_path / "capture.json"
    fake_claude = tmp_path / "fc.py"
    _write_fake_claude(
        fake_claude,
        f"CONFIRM_SWITCH = {confirm_switch}\nEXPECTED_SUBMITS = {expected_submits}\n"
        f"CANCEL_SWITCH = {cancel_switch}\n" + _INPUT_BOX_TUI,
    )
    result = claude_pty.run_claude_pty(
        [sys.executable, str(fake_claude), str(socket_path), str(capture)],
        route_prompt=_route_sonnet,
        socket_path=socket_path,
    )
    assert result == 0
    return json.loads(capture.read_text())["submitted"]


class TestClearInputKeys:
    def test_covers_each_line_and_line_break(self):
        # Claude Code 2.1.296 needs 2 * lines - 1 presses for a multi-line prompt.
        assert len(claude_pty.clear_input_keys("one\ntwo\nthree", 140)) >= 5

    def test_covers_soft_wrapped_lines(self):
        # A 343-character prompt in 140 columns needed 4 presses on Claude Code 2.1.296.
        prompt = "ROUTEME " + " ".join(f"word{i:03d}" for i in range(42))
        assert len(prompt) == 343
        assert len(claude_pty.clear_input_keys(prompt, 140)) >= 4


class TestBlockedPromptLeftInInputBox:
    def test_confirmed_cancellation_clears_the_populated_input_box(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.05)

        submitted = _run_input_box_tui(
            tmp_path, confirm_switch=False, expected_submits=2, cancel_switch=True
        )

        assert submitted == ["/model sonnet", "fix the parser\nthen run the tests"]

    def test_model_switch_runs_as_a_command_and_replay_is_exact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        submitted = _run_input_box_tui(tmp_path, confirm_switch=True, expected_submits=2)

        assert submitted == ["/model sonnet", "fix the parser\nthen run the tests"]

    def test_ready_timeout_replay_is_not_duplicated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 100.0)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.3)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        submitted = _run_input_box_tui(tmp_path, confirm_switch=False, expected_submits=1)

        assert submitted == ["fix the parser\nthen run the tests"]


# ---------------------------------------------------------------------------
# Test F: serve_first_prompt_socket calls on_submitted_prompt after claim
# ---------------------------------------------------------------------------


class TestOnSubmittedPromptCallback:
    def test_callback_fires_on_post_claim_route_request(self, tmp_path):
        socket_path = tmp_path / "first.sock"
        submitted: list[str] = []
        stop = threading.Event()
        ready = threading.Event()

        claude_pty.serve_first_prompt_socket(
            socket_path,
            lambda _p: claude_pty.FirstPromptRoute(
                model="sonnet", display_model="sonnet", rationale=""
            ),
            lambda _prompt, _model: None,
            stop,
            on_submitted_prompt=lambda prompt: submitted.append(prompt),
            # Wait on [READY], logged after listen(); the socket file exists from bind().
            log=lambda message: ready.set() if message.startswith("[READY]") else None,
        )
        try:
            assert ready.wait(5)

            # First request: blocked (claimed).
            r1 = claude_pty.request_first_prompt_route(
                socket_path, {"prompt": "fix the parser", "session_id": "s1"}
            )
            # Second request: allowed; on_submitted_prompt must be called.
            r2 = claude_pty.request_first_prompt_route(
                socket_path, {"prompt": "fix the parser", "session_id": "s1"}
            )
            # Give the server thread a moment to call the callback.
            deadline2 = time.monotonic() + 1.0
            while not submitted and time.monotonic() < deadline2:
                time.sleep(0.01)
        finally:
            stop.set()

        assert r1 is not None and r1["action"] == "block"
        assert r2 is not None and r2["action"] == "allow"
        assert submitted == ["fix the parser"], "on_submitted_prompt must fire once after claim"

    def test_callback_not_fired_for_initial_block(self, tmp_path):
        socket_path = tmp_path / "first.sock"
        submitted: list[str] = []
        stop = threading.Event()
        ready = threading.Event()

        claude_pty.serve_first_prompt_socket(
            socket_path,
            lambda _p: claude_pty.FirstPromptRoute(
                model="sonnet", display_model="sonnet", rationale=""
            ),
            lambda _prompt, _model: None,
            stop,
            on_submitted_prompt=lambda prompt: submitted.append(prompt),
            log=lambda message: ready.set() if message.startswith("[READY]") else None,
        )
        try:
            assert ready.wait(5)

            claude_pty.request_first_prompt_route(
                socket_path, {"prompt": "fix the parser", "session_id": "s1"}
            )
            time.sleep(0.05)  # small pause so any spurious callback could fire
        finally:
            stop.set()

        assert submitted == [], "on_submitted_prompt must not fire for the initial block"


class TestOpaqueInputRecovery:
    @pytest.mark.parametrize(
        "command, prompt, outcome",
        [
            (b"\x1b[A\r", "fix the bug", None),
            (b"/model\r", None, b"Set model to fable\n"),
            (b"/model\r", None, b"Set model to fable for this session only\n"),
            (b"/model\r", None, b"Kept model as haiku\n"),
            (b"\x1b[A\r", None, b"<session-clear>"),
        ],
        ids=[
            "history-prompt",
            "picker-choice",
            "picker-session-only",
            "picker-cancel",
            "history-clear-session",
        ],
    )
    def test_opaque_input_waits_for_native_classification(
        self, replay_model_switch, tmp_path, command, prompt, outcome
    ):
        body = (
            f"COMMAND = {command!r}\nPROMPT = {prompt!r}\nOUTCOME = {outcome!r}\n"
            + r"""
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
os.write(1, b"Set model to sonnet\n")
read_until(b"\x1b[201~")
assert os.read(0, 1) == b"\r"
assert send_hook(socket_path, "hello")["action"] == "allow"
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
assert (test_dir / "restored").exists()
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("opus")
    (test_dir / "ready").touch()
    assert read_until(b"\r") == COMMAND
    assert other._recovery_path.exists(), "opaque input discarded peer recovery"
    recovery = json.loads(other._recovery_path.read_text())
    assert other._token in recovery["pending"]
    assert recovery["before"] == {"model": "haiku"}
    saves_default = OUTCOME and OUTCOME.startswith(b"Set model") and b"for this session only" not in OUTCOME
    if not saves_default:
        settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
        deadline = time.monotonic() + 3
        while not other.is_routed():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert other.restore() is False, "a possible manual choice must settle first"
    if PROMPT is not None:
        assert send_hook(socket_path, PROMPT)["action"] == "allow"
    elif OUTCOME == b"<session-clear>":
        from ucode.smart_routing.claude_pty import request_first_prompt_route
        request_first_prompt_route(Path(socket_path), {
            "hook_event_name": "SessionStart", "source": "clear", "session_id": "s2",
        })
    else:
        if saves_default:
            from ucode import agents
            import subprocess
            helper = Path(agents.__file__).parent / "claude_mods" / "model-setting-receipt.py"
            def receipt(payload):
                response = subprocess.run(
                    [sys.executable, str(helper), socket_path], input=json.dumps(payload),
                    text=True, capture_output=True, timeout=10,
                )
                assert response.returncode == 0, response.stderr
                assert json.loads(response.stdout)["acknowledged"]
            receipt({"event": "command", "id": 1, "command": "model", "origin": "composer"})
            receipt({"event": "prepare", "id": 2, "command_id": 1})
            settings.write_text(json.dumps({"model": "fable", "unrelated": "preserved"}))
            receipt({"event": "saved", "id": 2, "model": "fable"})
            settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
        os.write(1, OUTCOME[:5])
        os.write(1, OUTCOME[5:])
    deadline = time.monotonic() + 3
    while not other.restore():
        assert time.monotonic() < deadline, "native classification did not release recovery"
        time.sleep(0.01)
    capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
finally:
    other.finish()
"""
        )
        with _stdin_after(tmp_path / "ready", [command]):
            capture, settings, _ = replay_model_switch(body)
        saved = (
            outcome and outcome.startswith(b"Set model") and b"for this session only" not in outcome
        )
        expected = "fable" if saved else "haiku"
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": expected, "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        "output, expected",
        [
            (b"Set model to fable\n", "confirmed"),
            (b"Model set to a custom label\n", "confirmed"),
            (b"Set model to\n", None),
            (b"Set model to fable", None),
            (b"Switch model? Yes, switch to fable\n", None),
            ("❯ Set model to fable\n".encode(), None),
            (b"Please explain: Set model to fable\n", None),
            ("  ⎿  Set model to fable\n".encode(), "confirmed"),
            ("  ⎿  Kept model as haiku\n".encode(), "cancelled"),
        ],
    )
    def test_opaque_input_needs_a_complete_native_result(self, output, expected):
        detector = claude_pty.ManualModelConfirmation(None)
        observations = [detector.observe(bytes([byte])) for byte in output]
        assert next((result for result in observations if result is not None), None) == expected


class TestOpaqueInputAcknowledgements:
    @pytest.mark.parametrize(
        "clear_session", [False, True], ids=["ordinary-prompt", "clear-session"]
    )
    def test_non_model_input_keeps_its_own_delayed_routed_write_recoverable(
        self, replay_model_switch, tmp_path, clear_session
    ):
        body = (
            f"CLEAR_SESSION = {clear_session!r}\n"
            + r"""
settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
(test_dir / "ready").touch()
assert read_until(b"\r") == b"\x1b[A\r"
recovery_path = next((Path.home() / ".ucode").glob("claude-v2-model-*.json"))
assert json.loads(recovery_path.read_text())["pending"]
if CLEAR_SESSION:
    from ucode.smart_routing.claude_pty import request_first_prompt_route
    request_first_prompt_route(Path(socket_path), {
        "hook_event_name": "SessionStart", "source": "clear", "session_id": "s2",
    })
else:
    assert send_hook(socket_path, "a different ordinary prompt")["action"] == "allow"
deadline = time.monotonic() + 3
while "manual" in json.loads(recovery_path.read_text()):
    assert time.monotonic() < deadline
    time.sleep(0.01)
assert json.loads(recovery_path.read_text())["pending"]
assert json.loads(settings.read_text())["model"] == "haiku"
assert not (test_dir / "restored").exists()
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
"""
        )
        with _stdin_after(tmp_path / "ready", [b"\x1b[A\r"]):
            capture, settings, _ = replay_model_switch(body)
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )

    @pytest.mark.parametrize(
        "old_clear, ordinary_prompt, reply",
        [
            pytest.param(False, False, b"Kept model as haiku\n", id="replay-ack-model-cancel"),
            pytest.param(True, False, b"Kept model as haiku\n", id="clear-session-model-cancel"),
            pytest.param(False, True, b"Here is the explanation.\n", id="replay-ack-prompt-text"),
        ],
    )
    def test_prior_native_event_does_not_settle_a_new_opaque_choice(
        self, replay_model_switch, tmp_path, old_clear, ordinary_prompt, reply
    ):
        body = (
            f"OLD_CLEAR = {old_clear!r}\nORDINARY_PROMPT = {ordinary_prompt!r}\nREPLY = {reply!r}\n"
            + r"""
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
os.write(1, b"Set model to sonnet\n")
read_until(b"\x1b[201~")
assert os.read(0, 1) == b"\r"
if OLD_CLEAR:
    from ucode.smart_routing.claude_pty import request_first_prompt_route
    assert send_hook(socket_path, "hello")["action"] == "allow"
    request_first_prompt_route(Path(socket_path), {
        "hook_event_name": "SessionStart", "source": "clear", "session_id": "s2",
    })
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("opus")
    (test_dir / "ready").touch()
    assert read_until(b"\r") == b"\x1b[A\r"
    settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
    deadline = time.monotonic() + 3
    while not other.is_routed():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert other.restore() is False
    if not OLD_CLEAR:
        assert send_hook(socket_path, "hello")["action"] == "allow"
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        assert other.restore() is False
        time.sleep(0.01)
    assert "manual" in json.loads(other._recovery_path.read_text())
    if ORDINARY_PROMPT:
        assert send_hook(socket_path, "quote the model result")["action"] == "allow"
    os.write(1, REPLY)
    deadline = time.monotonic() + 3
    while not other.restore():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
finally:
    other.finish()
"""
        )
        with _stdin_after(tmp_path / "ready", [b"\x1b[A\r"]):
            capture, settings, _ = replay_model_switch(body)
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )


class TestOpaqueInputHookOrder:
    @pytest.mark.parametrize("reply", [b"Set model to fable\n", b"```\nModel set to fable\n```\n"])
    def test_ordinary_prompt_classification_precedes_reply_text(
        self, replay_model_switch, tmp_path, reply
    ):
        # Synchronize the real select so the hook and reply arrive in the same PTY turn.
        setup = r"""
serve = claude_pty.serve_first_prompt_socket

def observe_submission(*args, **kwargs):
    submitted = kwargs["on_submitted_prompt"]
    def record(prompt):
        submitted(prompt)
        if prompt == "quote the model result":
            (root / "ordinary-classified").touch()
    kwargs["on_submitted_prompt"] = record
    return serve(*args, **kwargs)

claude_pty.serve_first_prompt_socket = observe_submission
native_select = claude_pty.select.select

def wait_for_reply(readers, writers, errors, timeout):
    if (root / "arm-reply").exists() and not (root / "reply-read-ready").exists():
        (root / "reply-read-ready").touch()
        timeout = 3
    return native_select(readers, writers, errors, timeout)

claude_pty.select.select = wait_for_reply
"""
        body = (
            f"REPLY = {reply!r}\n"
            + r"""
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
os.write(1, b"Set model to sonnet\n")
read_until(b"\x1b[201~")
assert os.read(0, 1) == b"\r"
assert send_hook(socket_path, "hello")["action"] == "allow"
other = v2._ClaudeModelSettingGuard(settings)
try:
    assert other.begin("opus")
    (test_dir / "ready").touch()
    assert read_until(b"\r") == b"\x1b[A\r"
    settings.write_text(json.dumps({"model": "opus", "unrelated": "preserved"}))
    deadline = time.monotonic() + 3
    while not other.is_routed():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert other.restore() is False
    (test_dir / "arm-reply").touch()
    deadline = time.monotonic() + 3
    while not (test_dir / "reply-read-ready").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert send_hook(socket_path, "quote the model result")["action"] == "allow"
    deadline = time.monotonic() + 3
    while not (test_dir / "ordinary-classified").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    os.write(1, REPLY)
    deadline = time.monotonic() + 3
    while not other.restore():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
finally:
    other.finish()
"""
        )
        with _stdin_after(tmp_path / "ready", [b"\x1b[A\r"]):
            capture, settings, observations = replay_model_switch(body, wrapper_setup=setup)
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )
        assert observations["errors"] == []


class TestPromptHookAcknowledgement:
    def test_prompt_is_classified_before_allow_is_sent(self, tmp_path):
        import socket

        socket_path = tmp_path / "first.sock"
        stop = threading.Event()
        ready = threading.Event()
        classified = threading.Event()
        release = threading.Event()

        def submitted(_prompt):
            classified.set()
            assert release.wait(5)

        server = claude_pty.serve_first_prompt_socket(
            socket_path,
            _route_sonnet,
            lambda _prompt, _model: None,
            stop,
            on_submitted_prompt=submitted,
            log=lambda message: ready.set() if message.startswith("[READY]") else None,
        )
        try:
            assert ready.wait(5)
            assert (
                claude_pty.request_first_prompt_route(socket_path, {"prompt": "first prompt"})[
                    "action"
                ]
                == "block"
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall(
                    (
                        json.dumps({"method": "route_first_prompt", "prompt": "ordinary prompt"})
                        + "\n"
                    ).encode()
                )
                assert classified.wait(5)
                # The native child must not start its reply before classification completes.
                client.settimeout(0.1)
                with pytest.raises(TimeoutError):
                    client.recv(1)
                release.set()
                client.settimeout(3)
                with client.makefile("rb") as stream:
                    assert json.loads(stream.readline()) == {"action": "allow"}
        finally:
            release.set()
            stop.set()
            server.join(timeout=2)
        assert not server.is_alive()


def test_prompt_recovery_saves_a_private_copy_for_later(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pty, "APP_DIR", tmp_path)
    prompt = 'Keep this "quoted" prompt.\r\n\x1b[2J\x7f'

    note = claude_pty.prompt_recovery_note(prompt)

    saved = list(tmp_path.glob("claude-prompt-*.txt"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == prompt.encode()
    assert saved[0].stat().st_mode & 0o777 == 0o600
    assert str(saved[0]) in note and "then delete the recovery file" in note


def test_prompt_recovery_full_directory_preserves_every_existing_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pty, "APP_DIR", tmp_path)
    original = {
        tmp_path / f"claude-prompt-{index}.txt": f"unrecovered prompt {index}"
        for index in range(claude_pty.MAX_PROMPT_RECOVERY_FILES)
    }
    for path, content in original.items():
        path.write_text(content)
    unrelated = tmp_path / "user-notes.txt"
    unrelated.write_text("user owned")
    prompt = 'Keep this "quoted" prompt.\n\x1b[2J\x7f'

    note = claude_pty.prompt_recovery_note(prompt)

    assert "recovery file limit" in note
    assert json.loads(note.split("JSON string: ", 1)[1]) == prompt
    assert "\x1b" not in note and "\x7f" not in note and "\n" not in note
    assert set(tmp_path.glob("claude-prompt-*.txt")) == set(original)
    assert all(path.read_text() == content for path, content in original.items())
    assert unrelated.read_text() == "user owned"


def test_prompt_recovery_preserves_unknown_links_when_full(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pty, "APP_DIR", tmp_path)
    target = tmp_path / "user-input.txt"
    target.write_text("user owned")
    links = [
        tmp_path / f"claude-prompt-unknown-{index}.txt"
        for index in range(claude_pty.MAX_PROMPT_RECOVERY_FILES)
    ]
    for path in links:
        path.symlink_to(target)

    note = claude_pty.prompt_recovery_note("recover later")

    assert json.loads(note.split("JSON string: ", 1)[1]) == "recover later"
    assert set(tmp_path.glob("claude-prompt-*.txt")) == set(links)
    assert all(path.is_symlink() and path.readlink() == target for path in links)
    assert target.read_text() == "user owned"


def test_prompt_recovery_uses_inline_copy_when_another_writer_holds_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pty, "APP_DIR", tmp_path)
    with (tmp_path / "claude-prompt-recovery.lock").open("a+") as lock:
        claude_pty.acquire_exclusive_file_lock(lock, timeout=0)
        try:
            note = claude_pty.prompt_recovery_note("recover after contention")
        finally:
            claude_pty.release_file_lock(lock)

    assert list(tmp_path.glob("claude-prompt-*.txt")) == []
    assert json.loads(note.split("JSON string: ", 1)[1]) == "recover after contention"


def test_prompt_recovery_concurrent_writers_respect_the_file_limit(tmp_path):
    copies = tmp_path / "copies"
    copies.mkdir()
    original = {
        copies / f"claude-prompt-existing-{index}.txt": f"preserved {index}"
        for index in range(claude_pty.MAX_PROMPT_RECOVERY_FILES - 1)
    }
    for path, content in original.items():
        path.write_text(content)
    ready = tmp_path / "ready"
    ready.mkdir()
    script = """\
import json
import os
import sys
import time
from pathlib import Path
from ucode.smart_routing import claude_pty

claude_pty.APP_DIR = Path(sys.argv[1])
ready = Path(sys.argv[2])
(ready / sys.argv[3]).touch()
deadline = time.monotonic() + 10
while len(list(ready.iterdir())) != 4:
    assert time.monotonic() < deadline, 'writers did not reach the barrier'
    time.sleep(0.005)
os.umask(0)
print(json.dumps(claude_pty.prompt_recovery_note(sys.argv[4])), flush=True)
"""
    processes = []
    notes = []
    try:
        for index in range(4):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(copies),
                        str(ready),
                        str(index),
                        f"prompt {index}",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            notes.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    saved = set(copies.glob("claude-prompt-*.txt"))
    assert len(saved) == claude_pty.MAX_PROMPT_RECOVERY_FILES
    assert all(path.read_text() == content for path, content in original.items())
    new = saved - set(original)
    assert len(new) == 1
    recovered = new.pop()
    assert recovered.stat().st_mode & 0o777 == 0o600
    assert sum(str(recovered) in note for note in notes) == 1
    assert {recovered.read_text()} | {
        json.loads(note.split("JSON string: ", 1)[1]) for note in notes if "JSON string: " in note
    } == {f"prompt {index}" for index in range(4)}


@pytest.mark.parametrize("persisted_before", [False, True], ids=["late-write", "already-written"])
@pytest.mark.parametrize("rejected", [False, True], ids=["cancelled", "premodel-rejection"])
def test_cancelled_manual_switch_keeps_its_own_routed_write_recoverable(
    replay_model_switch, tmp_path, persisted_before, rejected
):
    manual_model = "opus"
    body = (
        f"MANUAL_MODEL = {manual_model!r}\nPERSISTED_BEFORE = {persisted_before!r}\n"
        f"REJECTED = {rejected!r}\n"
        + r"""
settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
if PERSISTED_BEFORE:
    settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
(test_dir / "ready").touch()
command = read_until(b"\r")
assert command == f"/model {MANUAL_MODEL}\r".encode()
recovery_paths = list((Path.home() / ".ucode").glob("claude-v2-model-*.json"))
assert len(recovery_paths) == 1, "manual command discarded the pending routed write"
assert json.loads(recovery_paths[0].read_text())["pending"], "manual command discarded the pending routed write"
if REJECTED:
    os.write(1, f"Model switch to {MANUAL_MODEL} was blocked by a PreModelSwitch hook: policy\n".encode())
else:
    os.write(1, b"Model switch cancelled\n")
if not PERSISTED_BEFORE:
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        assert not (test_dir / "restored").exists(), "cancellation acknowledged a future write"
        assert json.loads(settings.read_text())["model"] == "haiku"
        time.sleep(0.01)
    settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
    os.write(1, b"Set model to sonnet\n")
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
assert not sel.select([0], [], [], 0.1)[0], "cancellation resumed automatic input"
capture_path.write_text(json.dumps({
    "command": command.hex(), "settings_while_running": json.loads(settings.read_text()),
}))
"""
    )
    with _stdin_after(tmp_path / "ready", [f"/model {manual_model}\r".encode()]):
        capture, settings, _ = replay_model_switch(body)

    assert capture["command"] == f"/model {manual_model}\r".encode().hex()
    assert (
        capture["settings_while_running"]
        == settings
        == {"model": "haiku", "unrelated": "preserved"}
    )


class TestLateAutomaticModelResult:
    def test_late_automatic_result_cannot_confirm_a_history_prompt(
        self, replay_model_switch, tmp_path
    ):
        body = r"""
settings = Path(sys.argv[3])
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\r")
(test_dir / "ready").touch()
assert read_until(b"\r") == b"\x1b[A\r"
recovery_path = next((Path.home() / ".ucode").glob("claude-v2-model-*.json"))
assert json.loads(recovery_path.read_text())["pending"]
os.write(1, b"Set model to sonnet\n")
deadline = time.monotonic() + 0.5
while time.monotonic() < deadline:
    assert recovery_path.exists(), "a late automatic result consumed the history prompt"
    recovery = json.loads(recovery_path.read_text())
    assert recovery["pending"] and "manual" in recovery
    time.sleep(0.01)
assert send_hook(socket_path, "a different ordinary prompt")["action"] == "allow"
settings.write_text(json.dumps({"model": "sonnet", "unrelated": "preserved"}))
deadline = time.monotonic() + 3
while not (test_dir / "restored").exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
capture_path.write_text(json.dumps({"settings_while_running": json.loads(settings.read_text())}))
"""
        with _stdin_after(tmp_path / "ready", [b"\x1b[A\r"]):
            capture, settings, _ = replay_model_switch(body)
        assert (
            capture["settings_while_running"]
            == settings
            == {"model": "haiku", "unrelated": "preserved"}
        )


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_failed_prompt_copy_does_not_use_a_slot(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(claude_pty, "APP_DIR", tmp_path)
    retained = tmp_path / "claude-prompt-retained.txt"
    retained.write_text("an earlier prompt")
    original = claude_pty.NamedTemporaryFile

    @contextmanager
    def failing_file(**kwargs):
        with original(**kwargs) as saved:

            class PartialWrite:
                name = saved.name

                def write(self, text):
                    saved.write(text[:3])
                    saved.flush()
                    raise OSError("disk full during prompt write")

            yield PartialWrite()

    def failed_sync(_fd):
        raise OSError("prompt could not be persisted")

    if failure == "write":
        monkeypatch.setattr(claude_pty, "NamedTemporaryFile", failing_file)
    else:
        monkeypatch.setattr(claude_pty.os, "fsync", failed_sync)
    prompt = "resend this entire prompt"
    note = claude_pty.prompt_recovery_note(prompt)
    assert json.loads(note.split("JSON string: ", 1)[1]) == prompt
    assert list(tmp_path.glob("claude-prompt-*.txt")) == [retained]
    assert retained.read_text() == "an earlier prompt"


@pytest.mark.parametrize("already_expired", [False, True], ids=["queued", "socket-backlog"])
def test_expired_model_receipt_does_not_change_recovery(replay_model_switch, already_expired):
    setup = r"""
claude_pty.MODEL_SETTING_RECEIPT_TIMEOUT_S = 0.05
native_select = claude_pty.select.select
paused = False

def pause_receipt_processing(readers, writers, errors, timeout):
    global paused
    if (root / "arm-receipt").exists() and not paused:
        paused = True
        (root / "receipt-paused").touch()
        deadline = time.monotonic() + 7
        while not (root / "receipt-expired").exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
    elif paused:
        (root / "receipt-drained").touch()
    return native_select(readers, writers, errors, timeout)

claude_pty.select.select = pause_receipt_processing
"""
    body = (
        f"ALREADY_EXPIRED = {already_expired}\n"
        + r"""
from ucode.smart_routing import v2

settings = Path(sys.argv[3])
peer = v2._ClaudeModelSettingGuard(settings)
try:
    assert peer.begin("sonnet")
    before = json.loads(peer._recovery_path.read_text())
    (test_dir / "arm-receipt").touch()
    deadline = time.monotonic() + 3
    while not (test_dir / "receipt-paused").exists():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(6)
        client.connect(socket_path)
        request = {"method": "model_setting", "event": "prepare", "id": 1}
        if ALREADY_EXPIRED:
            request["deadline"] = time.monotonic() - 1
        client.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(client.makefile("rb").readline())
    (test_dir / "receipt-expired").touch()
    deadline = time.monotonic() + 3
    while not (test_dir / "receipt-drained").exists():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    capture_path.write_text(json.dumps({
        "response": response,
        "before": before,
        "after": json.loads(peer._recovery_path.read_text()),
    }))
finally:
    peer.finish(timeout=0)
"""
    )
    capture, settings, observations = replay_model_switch(body, wrapper_setup=setup)
    assert capture["after"] == capture["before"]
    assert capture["response"] == {"acknowledged": False}
    assert settings == {"model": "haiku", "unrelated": "preserved"}
    assert observations["errors"] == []
