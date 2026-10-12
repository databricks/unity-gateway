"""Focused tests for the PTY paste/submit and fail-open fixes in claude_pty.py.

These tests cover:
- Paste and Enter are separate writes with PASTE_SUBMIT_DELAY_S gap (Bug A fix).
- Enter is retried when no submission signal arrives (Bug B fix).
- Enter stops retrying once the submission signal fires (Bug B fix).
- Switch-timeout path submits the prompt (Bug C fix).
- Persist-timeout path does not SIGTERM and submits (Bug C fix).
- waiting_to_switch timeout submits without /model (Bug D fix).
"""

from __future__ import annotations

import json
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
    def test_switch_timeout_sends_esc_then_pastes_and_submits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        socket_path = tmp_path / "first.sock"
        capture = tmp_path / "capture.json"
        restore_called: list[bool] = []
        fake_claude = tmp_path / "fc.py"
        _write_fake_claude(
            fake_claude,
            """\
resp = send_hook(socket_path, "hello")
assert resp["action"] == "block"
print("blocked", flush=True)
tty.setraw(0)

read_until(b"\\r")    # /model command — but we never confirm the switch

# After SWITCH_TIMEOUT_S: ESC is sent (to cancel /model), then paste, then CR.
esc_byte = os.read(0, 1)
paste = read_until(b"\\x1b[201~")
enter = os.read(0, 1)

capture_path.write_text(json.dumps({
    "esc_received": esc_byte == b"\\x1b",
    "paste_has_payload": b"\\x1b[200~" in paste,
    "enter_is_cr": enter == b"\\r",
}))
""",
        )

        result = claude_pty.run_claude_pty(
            [sys.executable, str(fake_claude), str(socket_path), str(capture)],
            route_prompt=_route_sonnet,
            socket_path=socket_path,
            restore_model_setting=lambda: restore_called.append(True),
        )

        assert result == 0
        assert len(restore_called) == 1, (
            "restore_model_setting must be called once on switch timeout"
        )
        data = json.loads(capture.read_text())
        assert data["esc_received"], "ESC must be sent to cancel /model"
        assert data["paste_has_payload"], "prompt must be pasted after switch timeout"
        assert data["enter_is_cr"], "CR (Enter) must follow the paste"


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
        )

        # SIGTERM would yield exit code 128+15=143; clean exit must be 0.
        assert result == 0, f"process should not be SIGTERMed; got exit code {result}"
        assert len(restore_called) == 1, (
            "restore_model_setting must be called once on persist timeout"
        )
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
# Test F: serve_first_prompt_socket calls on_submitted_prompt after claim
# ---------------------------------------------------------------------------


class TestOnSubmittedPromptCallback:
    def test_callback_fires_on_post_claim_route_request(self, tmp_path):
        socket_path = tmp_path / "first.sock"
        submitted: list[str] = []
        stop = threading.Event()

        claude_pty.serve_first_prompt_socket(
            socket_path,
            lambda _p: claude_pty.FirstPromptRoute(
                model="sonnet", display_model="sonnet", rationale=""
            ),
            lambda _prompt, _model: None,
            stop,
            on_submitted_prompt=lambda prompt: submitted.append(prompt),
        )
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)

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

        claude_pty.serve_first_prompt_socket(
            socket_path,
            lambda _p: claude_pty.FirstPromptRoute(
                model="sonnet", display_model="sonnet", rationale=""
            ),
            lambda _prompt, _model: None,
            stop,
            on_submitted_prompt=lambda prompt: submitted.append(prompt),
        )
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)

            claude_pty.request_first_prompt_route(
                socket_path, {"prompt": "fix the parser", "session_id": "s1"}
            )
            time.sleep(0.05)  # small pause so any spurious callback could fire
        finally:
            stop.set()

        assert submitted == [], "on_submitted_prompt must not fire for the initial block"
