"""Real terminal/profile checks; no gateway inference or model responses."""

import pytest
from auto_session import auto_session


@pytest.mark.parametrize("dimensions", [(50, 110), (22, 60)], ids=["docked", "narrow-overflow"])
def test_auto_model_selection_and_native_effort(native_binary, tmp_path, dimensions):
    """Scenario: choose Auto, read native effort status, and adjust effort inline.

    Expected: arrows move between models even when the pane overflows; only
    inline effort commits through native /effort; model, routing and native
    /model remain consistent, and Escape preserves the selected Auto alias.
    This is a real terminal/settings check without inference.
    """
    fixture = auto_session(
        native_binary,
        tmp_path,
        ["claude-opus-5-5", "claude-sonnet-5-5"],
        {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5"},
        dimensions,
    )
    launch, observe, picker, catalog = (
        fixture.launch,
        fixture.observe,
        fixture.picker,
        fixture.catalog,
    )

    def pane_text(screen):
        # Claude 2.1.290 can use a narrow side pane. Join its wrapped text
        # independently of the main transcript, retaining the full shortcut label.
        return " ".join(
            line.split("│", 1)[1].strip("│ ") for line in screen.splitlines() if "│" in line
        )

    with launch("native-effort") as tui:
        tui.boot(timeout=45)
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        heading = "Choose your main model"  # The remainder wraps in a narrow native dock.
        if dimensions[1] < 80:
            # The inline pane is only a few rows tall here; model navigation must
            # reveal the focused row rather than scroll past it without selecting.
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "the initial narrow selection")
            tui.send("\x1b[B", "select Sonnet in the overflowing pane")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "Sonnet selected by Down")
            tui.send("\x1b[A", "select Opus in the overflowing pane")
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "Opus selected by Up")
            tui.send("\x1b[B", "return to Sonnet")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "Sonnet selected again")
            tui.send("\r", "save the arrow-selected model")
            tui.wait_for(lambda s: "Auto (Sonnet 5.5) saved" in s, "saved narrow selection")
            selected = observe(tui, "narrow-selected")
            assert selected["model"] == catalog["models"][1]["autoModel"]
            assert selected["routing"] == "1"
            tui.submit("/auto")
            tui.wait_for(lambda s: "❯ Sonnet 5.5" in s, "reopened narrow selector")
            tui.send("\x1b[A", "draft Opus without saving")
            tui.wait_for(lambda s: "❯ Opus 5.5" in s, "draft Opus selection")
            tui.send("\x1b", "cancel narrow selection")
            tui.wait_for(lambda s: "❯ Opus 5.5" not in s, "closed narrow selector")
            assert observe(tui, "narrow-cancelled")["model"] == selected["model"]
            tui.exit_normally()
            return
        tui.wait_for(
            lambda s: (
                heading in s and "Medium effort" in s and "h/l to adjust effort" in pane_text(s)
            ),
            "the Auto effort pane",
        )
        tui.send("l", "increase effort without moving model focus")
        tui.wait_for(lambda s: "High effort" in s, "the higher draft effort")
        tui.send("h", "decrease effort without moving model focus")
        tui.wait_for(lambda s: "Medium effort" in s, "the lower draft effort")
        tui.send("l", "draft high effort on Opus")
        tui.wait_for(lambda s: "High effort" in s, "the high Opus draft")
        tui.send("\x1b[B", "move down to Sonnet rather than scrolling the pane")
        tui.wait_for(
            lambda s: "❯ Sonnet 5.5" in s and "Auto effort" in s,
            "Sonnet keeps its unqueried native default",
        )
        tui.send("\x1b[A", "move up to Opus rather than scrolling the pane")
        tui.wait_for(
            lambda s: "❯ Opus 5.5" in s and "High effort" in s,
            "Opus selected with its preserved draft",
        )
        tui.send("\r", "save model with high effort")
        tui.wait_for(
            lambda s: heading not in s and "Auto (Opus 5.5) saved" in s, "saved Auto effort"
        )
        tui.wait_for(
            lambda s: s.count("Set model to Auto") == 2,
            "Auto confirmations from the picker and main-model selection",
        )
        current = observe(tui, "selected")
        assert current["model"] == catalog["models"][0]["autoModel"], current
        tui.submit("/effort status")
        tui.wait_for(lambda s: "Current effort level: high" in s, "native effort agrees")
        picker(tui)
        tui.submit("/auto")
        tui.wait_for(lambda s: heading in s, "reopened effort pane")
        tui.send("h", "draft lower effort")
        tui.wait_for(lambda s: "Medium effort" in s, "lower draft")
        tui.send("\x1b", "cancel the effort change")
        tui.wait_for(lambda s: heading not in s, "cancelled pane")
        tui.submit("/effort status")
        tui.wait_for(lambda s: "Current effort level: high" in s, "cancelled effort unchanged")
        assert observe(tui, "cancelled")["model"] == current["model"]
        tui.exit_normally()
