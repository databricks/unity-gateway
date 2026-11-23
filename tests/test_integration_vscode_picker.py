"""Unit checks for how the VS Code integration helper steps through Copilot Chat's model picker."""

import sys
import time
from types import SimpleNamespace

import pytest

from tests.integration.utils import vscode as vscode_module
from tests.integration.utils.vscode import VSCode

LABEL = "Claude Haiku 4.5"
# ug's extension switches the chat to its default model once Copilot Chat is on.
DEFAULT_BUTTON = "Models, Claude Opus 5.5"
EDITOR = {"state": "language models editor", "modelsEditor": True, "picker": None}
CLOSED = {"state": "picker closed", "modelsEditor": False, "picker": None}
NO_MATCH = {"state": "picker: no match", "modelsEditor": False, "picker": LABEL}
PICKED = {"state": "picked", "modelsEditor": False, "picker": None}


class ScriptedVSCode(VSCode):
    """A window whose screens follow the actions taken, as ``reactions`` lists them per state.

    With ``starts_copilot_chat``, Copilot Chat starts while the picked screen shows: its Copilot
    session and that screen replace it after a number of reads."""

    def __init__(
        self,
        screen: dict,
        reactions: dict[str, list[dict]],
        *,
        button: str = DEFAULT_BUTTON,
        starts_copilot_chat: dict | None = None,
    ):
        self.actions, self.states, self.started = [], [], time.monotonic()
        self.copilot_session_ready = starts_copilot_chat is None
        self.starts_copilot_chat, self.picked_reads = starts_copilot_chat, 0
        self.current = self._screen(screen, button)
        self.reactions = {state: list(screens) for state, screens in reactions.items()}
        self.page = SimpleNamespace(keyboard=SimpleNamespace(press=self._escape))
        self.code = SimpleNamespace(poll=lambda: None)

    @staticmethod
    def _screen(screen: dict, button: str) -> dict:
        return {
            "modelButton": button,
            "copilotChatOffNotice": False,
            "tour": None,
            "sessionTarget": None,
            "signedOutNotice": False,
            **screen,
        }

    def screen(self) -> dict:
        if self.starts_copilot_chat and self.current["state"] == "picked":
            self.picked_reads += 1
            if self.picked_reads == 20:
                started = {**self.starts_copilot_chat, "sessionTarget": "Copilot"}
                self.current = self._screen(started, DEFAULT_BUTTON)
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
    monkeypatch.setattr(vscode_module, "TURN_TIMEOUT_S", 1)
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


def test_pick_model_closes_a_language_models_editor_that_copilot_chat_opens_as_it_starts():
    # Copilot Chat starts after the picked screen held, and opens the second editor as it does.
    window = ScriptedVSCode(
        PICKED,
        {"language models editor": [CLOSED], "picker closed": [PICKED]},
        button=f"Models, {LABEL}",
        starts_copilot_chat=EDITOR,
    )

    window.pick_model(LABEL)

    assert window.actions == ["language models editor", "picker closed"]
    assert window.current["state"] == "picked"


class FakeLocator:
    """A Playwright locator stand-in whose click runs ``on_click``."""

    def __init__(self, on_click):
        self.on_click = on_click
        self.first = self.last = self

    def get_by_role(self, role, **kwargs):
        return self

    def locator(self, selector):
        return self

    def click(self, timeout=None):
        self.on_click()


class ScreenedVSCode(VSCode):
    """A window that shows ``screens`` in order, advancing on every click or key."""

    def __init__(self, screens: list[dict]):
        self.actions, self.states, self.started = [], [], time.monotonic()
        self.screens = list(screens)
        advance = self._advance
        self.page = SimpleNamespace(
            keyboard=SimpleNamespace(press=lambda key: advance()),
            mouse=SimpleNamespace(move=lambda x, y: None),
            locator=lambda selector: FakeLocator(advance),
        )
        self.code = SimpleNamespace(poll=lambda: None)

    def _advance(self) -> None:
        if len(self.screens) > 1:
            self.screens.pop(0)

    def screen(self) -> dict:
        return {
            "modelsEditor": False,
            "dropdown": False,
            "dialog": None,
            "confirmation": None,
            "request": None,
            "response": None,
            **self.screens[0],
        }

    def shot(self, name: str) -> None:
        pass


def test_closing_the_language_models_editor_dismisses_a_dropdown_first():
    window = ScreenedVSCode(
        [
            {"modelsEditor": True, "dropdown": True},
            {"modelsEditor": True, "dropdown": False},
            {"modelsEditor": False},
        ]
    )

    window._wait(window._close_models_editor(), 1, "the editor to close")

    assert window.actions == [
        "Language Models editor: Escape",
        "Language Models editor: Close",
    ]


PROMPT = "Read the local file input.txt in the current directory using a tool."
READ_CONFIRMATION = 'Read input.txt Input { "path": "/tmp/project/input.txt" } Allow Once Skip'


def test_wait_for_answer_allows_a_read_only_tool_once():
    window = ScreenedVSCode(
        [
            {
                "confirmation": READ_CONFIRMATION,
                "request": PROMPT,
                "response": {"busy": True, "text": "1 confirmation pending"},
            },
            {"request": PROMPT, "response": {"busy": False, "text": "42"}},
        ]
    )

    assert window.wait_for_answer(PROMPT) == "42"
    assert window.actions == ["tool confirmation: Allow Once"]


def test_wait_for_answer_refuses_a_confirmation_that_is_not_read_only():
    window = ScreenedVSCode(
        [
            {
                "confirmation": 'Run rm -rf build Input { "command": "rm -rf build" } Allow Once Skip',
                "request": PROMPT,
                "response": {"busy": True, "text": "1 confirmation pending"},
            }
        ]
    )

    with pytest.raises(vscode_module.UnknownScreen, match="Unexpected tool confirmation"):
        window.wait_for_answer(PROMPT)
    assert window.actions == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (READ_CONFIRMATION, True),
        ("Allow reading file outside of workspace? " + READ_CONFIRMATION, True),
        ("Read input.txt Input {} Skip", False),
        ("Edit input.txt Input {} Allow Once Skip", False),
    ],
)
def test_read_only_tool_confirmation(text, expected):
    assert vscode_module.read_only_tool_confirmation(text) is expected
