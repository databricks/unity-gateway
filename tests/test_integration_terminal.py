"""Component checks for observed native terminal layouts."""

from types import SimpleNamespace

import pytest

from tests.integration.utils.terminal import TerminalProcess, _claude_background_tasks_complete


@pytest.mark.parametrize(
    "history", ["", "Earlier in this session (0)", "Earlier in this session (1)"]
)
def test_completed_task_menu_accepts_collapsed_history(history):
    screen = f"Background\n\nCompleted (1)\n❯ ✔ calculate   done · Haiku 5.5\n{history}\n↑/↓ to select · Enter to view · Esc to close"
    assert _claude_background_tasks_complete(screen)
    assert not _claude_background_tasks_complete(screen.replace("done", "running"))
    assert not _claude_background_tasks_complete(screen.replace("Completed (1)", "Running (1)"))
    assert not _claude_background_tasks_complete(screen.replace("Completed (1)", "Completed (2)"))


def test_menu_selection_excludes_earlier_submitted_prompt():
    terminal = object.__new__(TerminalProcess)
    terminal.screen = SimpleNamespace(
        display=["❯ /model", "Select model", "  1. Default", "❯ 2. Auto"]
    )
    assert terminal.selected_line(after="Select model") == "❯ 2. Auto"
