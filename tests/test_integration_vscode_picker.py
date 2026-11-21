"""Unit checks for how the VS Code integration helper steps through Copilot Chat's model picker."""

import sys
import time
from types import SimpleNamespace

import pytest

from tests.integration.utils import vscode as vscode_module
from tests.integration.utils.vscode import VSCode

LABEL = "Claude Haiku 4.5"
EDITOR = {"state": "language models editor", "modelsEditor": True, "picker": None}
CLOSED = {"state": "picker closed", "modelsEditor": False, "picker": None}
NO_MATCH = {"state": "picker: no match", "modelsEditor": False, "picker": LABEL}
PICKED = {"state": "picked", "modelsEditor": False, "picker": None}


class ScriptedVSCode(VSCode):
    """A window whose screens follow the actions taken, as ``reactions`` lists them per state."""

    def __init__(self, screen: dict, reactions: dict[str, list[dict]]):
        self.actions, self.states, self.started = [], [], time.monotonic()
        self.copilot_chat_ready = True
        self.current = self._screen(screen, "Models, Claude Opus 5.5")
        self.reactions = {state: list(screens) for state, screens in reactions.items()}
        self.page = SimpleNamespace(keyboard=SimpleNamespace(press=self._escape))
        self.code = SimpleNamespace(poll=lambda: None)

    @staticmethod
    def _screen(screen: dict, button: str) -> dict:
        return {"modelButton": button, "copilotChatOffNotice": False, **screen}

    def screen(self) -> dict:
        return self.current

    def shot(self, name: str) -> None:
        pass

    def _picker_state(self, screen: dict, label: str) -> str:
        return screen["state"]

    def _picker_action(self, state: str, screen: dict, label: str) -> None:
        self.actions.append(state)
        following = self.reactions[state].pop(0)
        button = f"Models, {label}" if following is PICKED else screen["modelButton"]
        self.current = self._screen(following, button)

    def _escape(self, key: str) -> None:
        self.current = self._screen(CLOSED, self.current["modelButton"])


@pytest.fixture(autouse=True)
def fast_steps(monkeypatch):
    # The unit test environment has no Playwright; the helper only needs its timeout error.
    sync_api = SimpleNamespace(TimeoutError=type("TimeoutError", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(vscode_module, "STEP_TIMEOUT_S", 1)
    monkeypatch.setattr(vscode_module, "SETTLE_S", 0.05)
    monkeypatch.setattr(vscode_module, "POLL_S", 0.01)


def test_pick_model_closes_a_language_models_editor_that_opens_as_the_last_one_closes():
    # The second editor is already up on the first read after the first one closed.
    window = ScriptedVSCode(
        EDITOR, {"language models editor": [EDITOR, CLOSED], "picker closed": [PICKED]}
    )

    window.pick_model(LABEL)

    assert window.actions == ["language models editor", "language models editor", "picker closed"]


def test_assert_not_in_picker_closes_a_language_models_editor_that_opens_as_the_last_one_closes():
    window = ScriptedVSCode(
        EDITOR, {"language models editor": [EDITOR, CLOSED], "picker closed": [NO_MATCH]}
    )

    window.assert_not_in_picker(LABEL)

    assert window.actions == [
        "language models editor",
        "language models editor",
        "picker closed",
        "model picker: Escape",
    ]
    assert window.current["picker"] is None
