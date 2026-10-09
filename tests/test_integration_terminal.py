"""Validate native command approval screens without launching an agent."""

import pytest

from tests.integration.utils.terminal import codex_command_approval_pending

TOGGLE_COMMAND = '"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex --disable-smart-routing'


def _approval_screen(command=TOGGLE_COMMAND):
    return f"""
  Would you like to run the following command?

  Environment: local
  Reason: Allow updating this session's Smart Router controls?

  $ {command}

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with this command (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""


def test_codex_approval_matches_the_exact_requested_toggle():
    assert codex_command_approval_pending(_approval_screen(), TOGGLE_COMMAND)


@pytest.mark.parametrize(
    "command",
    [
        TOGGLE_COMMAND.replace("--disable-", "--enable-"),
        TOGGLE_COMMAND + "\n  touch /tmp/unexpected",
    ],
)
def test_codex_approval_rejects_different_or_additional_commands(command):
    with pytest.raises(AssertionError, match="unexpected command approval"):
        codex_command_approval_pending(_approval_screen(command), TOGGLE_COMMAND)


def test_codex_approval_requires_the_one_time_choice():
    screen = _approval_screen().replace("› 1.", "  1.").replace("  2.", "› 2.")
    with pytest.raises(AssertionError, match="unexpected command approval"):
        codex_command_approval_pending(screen, TOGGLE_COMMAND)


def test_codex_approval_waits_for_the_complete_dialog():
    screen = _approval_screen().split("Press enter to confirm")[0]
    assert not codex_command_approval_pending(screen, TOGGLE_COMMAND)
    assert not codex_command_approval_pending("Working (5s · esc to interrupt)", TOGGLE_COMMAND)
