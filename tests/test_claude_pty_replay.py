"""Focused tests for the PTY paste/submit and fail-open fixes in claude_pty.py.

These tests cover:
- Paste and Enter are separate writes with PASTE_SUBMIT_DELAY_S gap (Bug A fix).
- Enter is retried when no submission signal arrives (Bug B fix).
- Enter stops retrying once the submission signal fires (Bug B fix).
- Switch-timeout path submits the prompt after Claude settles (Bug C fix).
- Late model-command completion cannot discard the replay or overwrite the saved default.
- An unresponsive model command stops automatic input with a recovery message.
- Persist-timeout path does not SIGTERM and submits (Bug C fix).
- waiting_to_switch timeout submits without /model (Bug D fix).
- Every write into the input box first clears the blocked prompt that Claude Code
  keeps there, so /model runs as a command and the replay is not mangled (Bug E fix).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
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
import socket
import sys
import time
import tty
from pathlib import Path

socket_path = sys.argv[1]
capture_path = Path(sys.argv[2])

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
# Test C: Switch-timeout path submits the prompt (fail-open, Bug C)
# ---------------------------------------------------------------------------


class TestSwitchTimeoutFailOpen:
    @pytest.mark.parametrize("intermediate_redraw", [False, True], ids=["silent", "redraw"])
    @pytest.mark.parametrize(
        "persist_delay", [0.0, 0.12, 0.4], ids=["persisted", "pending-write", "late-write"]
    )
    def test_late_model_completion_cannot_discard_the_replayed_prompt(
        self, tmp_path, monkeypatch, persist_delay, intermediate_redraw
    ):
        from ucode.smart_routing import v2

        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SELECT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.15)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "SUBMIT_VERIFY_S", 0.15)
        monkeypatch.setattr(claude_pty, "MODEL_PERSIST_TIMEOUT_S", 0.25)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        settings = tmp_path / "settings.json"
        original_settings = {"model": "haiku", "unrelated": "preserved"}
        settings.write_text(json.dumps(original_settings))
        monkeypatch.setattr(v2, "APP_DIR", tmp_path)
        guard = v2._ClaudeModelSettingGuard(settings)
        restored_after_completion = []

        def restore():
            restored_after_completion.append(guard.is_routed())
            guard.restore()

        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            f"PERSIST_DELAY = {persist_delay}\nINTERMEDIATE_REDRAW = {intermediate_redraw}\n"
            + """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")

# Claude 2.1.290 can finish /model after the wrapper sends its timeout Escape.
# The command clears the input box when it finishes, discarding an early replay.
ready_at = None
model_complete = False
persist_at = None
received = b""
submitted = []
deadline = time.monotonic() + 3.0
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
    readable, _, _ = sel.select([0], [], [], 0.01)
    if not readable:
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
        prefix, rest = received.split(b"\\x1b[200~", 1)
        prompt, rest = rest.split(b"\\x1b[201~", 1)
        if b"\\r" in rest:
            submitted.append(prompt.decode())
            send_hook(socket_path, prompt.decode())
            received = b""
            deadline = min(deadline, time.monotonic() + 0.5)
capture_path.write_text(json.dumps({"submitted": submitted}))
""",
        )

        try:
            result = claude_pty.run_claude_pty(
                [sys.executable, str(fake_claude), str(socket_path), str(capture), str(settings)],
                route_prompt=_route_sonnet,
                socket_path=socket_path,
                prepare_model_switch=guard.begin,
                model_switch_persisted=guard.is_routed,
                restore_model_setting=restore,
                defer_model_setting=guard.defer,
                abandon_model_setting=guard.abandon,
            )
        finally:
            guard.restore()

        assert result == 0
        assert json.loads(capture.read_text())["submitted"] == ["hello"]
        assert restored_after_completion == [True]
        assert json.loads(settings.read_text()) == original_settings

    @pytest.mark.parametrize("later_model", ["user-selected", "sonnet"])
    def test_abandoned_switch_releases_guard_and_preserves_manual_choice(
        self, tmp_path, monkeypatch, later_model
    ):
        from ucode.smart_routing import v2

        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SELECT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.15)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.25)
        monkeypatch.setattr(v2, "APP_DIR", tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"model": "haiku", "unrelated": "preserved"}))
        guard = v2._ClaudeModelSettingGuard(settings)
        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            f"LATER_MODEL = {later_model!r}\n"
            + """\
import fcntl

assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
time.sleep(0.5)
settings = Path(sys.argv[3])
with (settings.parent / "claude-v2-model.lock").open("a+") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_available = True
    except BlockingIOError:
        lock_available = False
saved = json.loads(settings.read_text())
saved["model"] = LATER_MODEL
settings.write_text(json.dumps(saved))
if LATER_MODEL == "sonnet":
    os.write(1, b"Set model to sonnet\\n")
received = b""
deadline = time.monotonic() + 0.1
while time.monotonic() < deadline:
    if sel.select([0], [], [], 0.01)[0]:
        received += os.read(0, 4096)
capture_path.write_text(json.dumps({"lock_available": lock_available, "received": received.hex()}))
""",
        )

        try:
            result = claude_pty.run_claude_pty(
                [sys.executable, str(fake_claude), str(socket_path), str(capture), str(settings)],
                route_prompt=_route_sonnet,
                socket_path=socket_path,
                prepare_model_switch=guard.begin,
                model_switch_persisted=guard.is_routed,
                restore_model_setting=guard.restore,
                defer_model_setting=guard.defer,
                abandon_model_setting=guard.abandon,
            )
        finally:
            guard.restore()

        assert result == 0
        assert json.loads(capture.read_text())["lock_available"] is True
        assert json.loads(capture.read_text())["received"] == ""
        assert json.loads(settings.read_text()) == {
            "model": "haiku" if later_model == "sonnet" else "user-selected",
            "unrelated": "preserved",
        }

    def test_manual_selection_of_the_routed_model_survives_recovery(self, tmp_path, monkeypatch):
        from ucode.smart_routing import v2

        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SELECT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.15)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(v2, "APP_DIR", tmp_path)
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"model": "haiku"}))
        ready = tmp_path / "ready"
        guard = v2._ClaudeModelSettingGuard(settings)
        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
assert send_hook(socket_path, "hello")["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
assert os.read(0, 1) == b"\\x1b"
time.sleep(0.5)
Path(sys.argv[4]).touch()
command = read_until(b"\\r")
Path(sys.argv[3]).write_text(json.dumps({"model": "sonnet"}))
os.write(1, b"Set model to sonnet\\n")
time.sleep(0.1)
capture_path.write_text(json.dumps({"command": command.decode()}))
""",
        )
        pipe_read, pipe_write = os.pipe()
        saved_stdin = os.dup(0)

        def select_model():
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            os.write(pipe_write, b"/model sonnet\r")

        writer = threading.Thread(target=select_model)
        try:
            os.dup2(pipe_read, 0)
            writer.start()
            result = claude_pty.run_claude_pty(
                [
                    sys.executable,
                    str(fake_claude),
                    str(socket_path),
                    str(capture),
                    str(settings),
                    str(ready),
                ],
                route_prompt=_route_sonnet,
                socket_path=socket_path,
                prepare_model_switch=guard.begin,
                model_switch_persisted=guard.is_routed,
                restore_model_setting=guard.restore,
                defer_model_setting=guard.defer,
                abandon_model_setting=guard.abandon,
            )
        finally:
            os.dup2(saved_stdin, 0)
            writer.join(timeout=4)
            os.close(saved_stdin)
            os.close(pipe_read)
            os.close(pipe_write)
            guard.restore()

        assert result == 0
        assert not writer.is_alive()
        assert json.loads(capture.read_text())["command"] == "/model sonnet\r"
        assert json.loads(settings.read_text()) == {"model": "sonnet"}

    @pytest.mark.parametrize("keeps_rendering", [False, True], ids=["silent", "busy"])
    def test_unresponsive_switch_stops_automatic_input(
        self, tmp_path, monkeypatch, keeps_rendering
    ):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SELECT_TIMEOUT_S", 0.02)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.15)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.25)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        log = tmp_path / "routing.log"
        restore_called = []
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            f"KEEPS_RENDERING = {keeps_rendering}\n"
            + """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
tty.setraw(0)
print("blocked", flush=True)
read_until(b"\\r")
esc = os.read(0, 1)

# No ready response: either silent or continuously rendering a busy command.
received = b""
deadline = time.monotonic() + 0.6
while time.monotonic() < deadline:
    if KEEPS_RENDERING:
        os.write(1, b"busy\\r")
    readable, _, _ = sel.select([0], [], [], 0.01)
    if readable:
        received += os.read(0, 4096)
capture_path.write_text(json.dumps({"esc": esc.hex(), "received": received.hex()}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
            model_switch_persisted=lambda: False,
            restore_model_setting=lambda: restore_called.append(True),
            log_path=log,
        )

        assert result == 0
        assert json.loads(capture.read_text()) == {"esc": "1b", "received": ""}
        assert restore_called == [], "defer restoration until Claude can no longer write the model"
        assert "automatic replay stopped" in log.read_text()

    def test_cancel_redraw_does_not_confirm_model_completion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.25)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        restore_called: list[bool] = []
        deferred: list[bool] = []
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")    # /model command — but we never confirm the switch

# The native TUI redraws after handling a standalone Escape.
esc_byte = os.read(0, 1)
os.write(1, b"Cancelled model switch\\n")
received = b""
deadline = time.monotonic() + 0.6
while time.monotonic() < deadline:
    if sel.select([0], [], [], 0.01)[0]:
        received += os.read(0, 4096)

capture_path.write_text(json.dumps({
    "esc_received": esc_byte == b"\\x1b",
    "received": received.hex(),
}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
            model_switch_persisted=lambda: False,
            restore_model_setting=lambda: restore_called.append(True),
            defer_model_setting=lambda: deferred.append(True),
        )

        assert result == 0
        assert restore_called == []
        assert deferred == [True]
        data = json.loads(capture.read_text())
        assert data["esc_received"], "ESC must be sent to cancel /model"
        assert data["received"] == "", "a redraw must not trigger automatic input"


# ---------------------------------------------------------------------------
# Test D: Persist-timeout path does not SIGTERM and submits (Bug C)
# ---------------------------------------------------------------------------


class TestPersistTimeoutFailOpen:
    def test_persist_timeout_does_not_sigterm_and_submits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "MODEL_PERSIST_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        restore_called: list[bool] = []
        deferred: list[bool] = []
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")                        # /model command
os.write(1, b"Set model to sonnet\\n")    # switch confirmed
sys.stdout.flush()

# model_switch_persisted() always returns False in this test,
# so after MODEL_PERSIST_TIMEOUT_S the loop must paste without SIGTERMing us.
paste = read_until(b"\\x1b[201~")
enter = os.read(0, 1)

capture_path.write_text(json.dumps({
    "paste_received": b"\\x1b[200~" in paste,
    "enter_is_cr": enter == b"\\r",
}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
            model_switch_persisted=lambda: False,  # persist check always fails
            restore_model_setting=lambda: restore_called.append(True),
            defer_model_setting=lambda: deferred.append(True),
        )

        # SIGTERM would yield exit code 128+15=143; clean exit must be 0.
        assert result == 0, f"process should not be SIGTERMed; got exit code {result}"
        assert restore_called == []
        assert deferred == [True]
        data = json.loads(capture.read_text())
        assert data["paste_received"], "prompt must be pasted after persist timeout"
        assert data["enter_is_cr"], "CR (Enter) must follow the paste"


# ---------------------------------------------------------------------------
# Test E: waiting_to_switch timeout submits without /model (Bug D)
# ---------------------------------------------------------------------------


class TestWaitingToSwitchTimeout:
    def test_ready_timeout_submits_without_model_switch(self, tmp_path, monkeypatch):
        # READY_QUIET_S >> READY_TIMEOUT_S so the quiet check never fires first.
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 100.0)
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

capture_path.write_text(json.dumps({
    "no_model_cmd_before_paste": b"/model" not in pre,
    "paste_received": True,
    "enter_is_cr": enter == b"\\r",
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
        assert data["no_model_cmd_before_paste"], (
            "no /model command should be sent when the quiet window times out"
        )
        assert data["paste_received"]
        assert data["enter_is_cr"]


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
in_hook = None
deadline = time.monotonic() + 3.0
while len(submitted) < EXPECTED_SUBMITS and time.monotonic() < deadline:
    if not sel.select([0], [], [], 0.05)[0]:
        continue
    pending += os.read(0, 1)
    if pending == b"\\x1b" and not sel.select([0], [], [], 0.03)[0]:
        if in_hook is not None:
            box, in_hook = in_hook, None
        pending = b""
        os.write(1, b"Cancelled model switch\\n")
        deadline = min(deadline, time.monotonic() + 0.6)
        continue
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
        if in_hook is not None:  # ESC during the hook restores the typed prompt
            box, in_hook = in_hook, None
        continue
    char, pending = pending.decode(), b""
    if char == "\\r" and not pasting:
        submitted.append(box)
        if box.startswith("/model ") and CONFIRM_SWITCH:
            os.write(1, b"Set model to sonnet\\n")
        in_hook = None if box.startswith("/") else box
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


def _run_input_box_tui(tmp_path: Path, *, confirm_switch: bool, expected_submits: int) -> list:
    socket_path = tmp_path / "first.sock"
    capture = tmp_path / "capture.json"
    fake_claude = tmp_path / "fc.py"
    _write_fake_claude(
        fake_claude,
        f"CONFIRM_SWITCH = {confirm_switch}\nEXPECTED_SUBMITS = {expected_submits}\n"
        + _INPUT_BOX_TUI,
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
    def test_model_switch_runs_as_a_command_and_replay_is_exact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        submitted = _run_input_box_tui(tmp_path, confirm_switch=True, expected_submits=2)

        assert submitted == ["/model sonnet", "fix the parser\nthen run the tests"]

    def test_switch_timeout_does_not_replay_before_completion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)
        monkeypatch.setattr(claude_pty, "READY_TIMEOUT_S", 0.25)

        submitted = _run_input_box_tui(tmp_path, confirm_switch=False, expected_submits=2)

        assert submitted == ["/model sonnet"]

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
