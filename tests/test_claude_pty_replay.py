"""Focused tests for the PTY paste/submit and fail-open fixes in claude_pty.py.

These tests cover:
- Paste and Enter are separate writes with PASTE_SUBMIT_DELAY_S gap (Bug A fix).
- Enter is retried when no submission signal arrives (Bug B fix).
- Enter stops retrying once the submission signal fires (Bug B fix).
- Switch-timeout path submits the prompt (Bug C fix).
- Persist-timeout path does not SIGTERM and submits (Bug C fix).
- waiting_to_switch timeout submits without /model (Bug D fix).
- Every write into the input box first clears the blocked prompt that Claude Code
  keeps there, so /model runs as a command and the replay is not mangled (Bug E fix).
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
while len(submitted) < EXPECTED_SUBMITS:
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

    def test_switch_timeout_replay_is_not_mangled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_pty, "READY_QUIET_S", 0.05)
        monkeypatch.setattr(claude_pty, "SWITCH_TIMEOUT_S", 0.25)
        monkeypatch.setattr(claude_pty, "ESC_GAP_S", 0.05)
        monkeypatch.setattr(claude_pty, "PASTE_SUBMIT_DELAY_S", 0.05)

        submitted = _run_input_box_tui(tmp_path, confirm_switch=False, expected_submits=2)

        assert submitted[-1] == "fix the parser\nthen run the tests"

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


class TestManualModelConfirmation:
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
            (b"Kept model as haiku\n", "cancelled"),
            (b"Model switch cancelled\n", "cancelled"),
        ],
    )
    def test_opaque_input_needs_a_complete_native_result(self, output, expected):
        detector = claude_pty.ManualModelConfirmation(None)
        observations = [detector.observe(bytes([byte])) for byte in output]
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
