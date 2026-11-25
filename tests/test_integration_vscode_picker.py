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
    monkeypatch.setattr(vscode_module, "SETTLE_S", 0.05)
    monkeypatch.setattr(vscode_module, "POLL_S", 0.01)


def test_model_picker_dismisses_topmost_dropdown_before_closing_editor():
    class Element:
        def __init__(self, window):
            self.window = window
            self.first = self

        def locator(self, *_args, **_kwargs):
            return self

        def filter(self, **_kwargs):
            return self

        def element_handle(self, **_kwargs):
            return self

        def is_visible(self):
            return self.window.current["modelsEditor"]

        def click(self, **_kwargs):
            assert not self.window.current["dropdown"], (
                "dropdown must be dismissed before editor close"
            )
            self.window.current["modelsEditor"] = False

    class OverlappingWindow(VSCode):
        def __init__(self):
            self.current = {
                "onboarding": None,
                "dialog": None,
                "tour": None,
                "copilotChatOffNotice": False,
                "modelsEditor": True,
                "dropdown": True,
                "picker": "Databricks model",
                "modelButton": f"Models, {LABEL}",
            }
            self.actions, self.states, self.started = [], [], time.monotonic()
            self.copilot_session_ready = True
            self.code = SimpleNamespace(poll=lambda: None)
            element = Element(self)
            self.page = SimpleNamespace(
                locator=lambda *_: element,
                keyboard=SimpleNamespace(press=self.dismiss),
                mouse=SimpleNamespace(move=lambda *_: None),
            )

        def screen(self):
            return dict(self.current)

        def dismiss(self, key):
            assert key == "Escape"
            self.current.update(dropdown=False, picker=None)

        def shot(self, _name):
            pass

    window = OverlappingWindow()
    assert window._picker_state(window.screen(), LABEL) == "dropdown blocking editor"
    window.pick_model(LABEL)
    assert window.actions == ["model picker: dismiss dropdown", "Language Models editor: Close"]
    assert not window.current["dropdown"] and not window.current["modelsEditor"]


@pytest.mark.parametrize(
    ("screen", "message"),
    [
        ({"modelsEditor": True, "dropdown": True}, "dropdown appeared before the click"),
        ({"modelsEditor": False, "dropdown": False}, "editor closed before the click"),
        ({"modelsEditor": True, "dropdown": False}, None),
    ],
)
def test_editor_close_timeout_reports_observed_transition(screen, message):
    class Element:
        first = None

        def __init__(self):
            self.first = self

        def filter(self, **_kwargs):
            return self

        def element_handle(self, **_kwargs):
            from playwright.sync_api import TimeoutError

            raise TimeoutError("click target changed")

    window = object.__new__(VSCode)
    window.actions = []
    window.page = SimpleNamespace(locator=lambda *_: Element())
    window.screen = lambda: screen
    if message is None:
        from playwright.sync_api import TimeoutError

        with pytest.raises(TimeoutError, match="click target changed"):
            window._close_models_editor()
        assert window.actions == []
    else:
        assert window._close_models_editor()()
        assert window.actions == [f"  ({message})"]


def test_pick_model_closes_a_language_models_editor_that_opens_as_the_last_one_closes():
    # The second editor is already up on the first read after the first one closed.
    window = ScriptedVSCode(
        EDITOR, {"language models editor": [EDITOR, CLOSED], "picker closed": [PICKED]}
    )

    window.pick_model(LABEL)

    assert window.actions == ["language models editor", "language models editor", "picker closed"]


def test_assert_not_in_picker_handles_dropdown_blocking_editor():
    overlap = {"state": "dropdown blocking editor", "modelsEditor": True, "picker": "open"}
    window = ScriptedVSCode(
        overlap,
        {
            "dropdown blocking editor": [EDITOR],
            "language models editor": [CLOSED],
            "picker closed": [NO_MATCH],
        },
    )
    window.assert_not_in_picker(LABEL)
    assert window.actions == [
        "dropdown blocking editor",
        "language models editor",
        "picker closed",
        "model picker: Escape",
    ]
    assert window.current["picker"] is None


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
