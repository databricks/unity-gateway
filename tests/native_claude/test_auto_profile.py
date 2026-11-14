"""Real terminal/profile checks; no gateway inference or model responses."""

import pytest
from auto_session import auto_session


@pytest.mark.parametrize(
    ("alternate", "alternate_label", "context_window"),
    [
        ("claude-haiku-4-5", "Haiku 4.5", 200_000),
        ("claude-sonnet-5-5", "Sonnet 5.5", 1_000_000),
        ("claude-fable-5-1", "Fable 5.1", 1_000_000),
        ("system.ai.gpt-6-astra[1m]", "Astra 6", 1_000_000),
    ],
)
def test_auto_switch_updates_native_profile_without_changing_another_session(
    native_binary, tmp_path, alternate, alternate_label, context_window
):
    """Scenario: change Auto's main model through UG's actual pane in one of two TUIs.

    Expected: one checked Auto row, updated native context and effort, the other
    session unchanged, cancellation preserved, and manual selection disables routing.
    Model-command echoes and confirmations use friendly Auto names.
    This exercises local model/UI metadata, not inference or an actual /compact call.
    """
    fixture = auto_session(
        native_binary,
        tmp_path,
        ["claude-opus-5-5", alternate],
        {"claude-opus-5-5": "Opus 5.5", alternate: alternate_label},
    )
    launch, observe, picker, catalog = (
        fixture.launch,
        fixture.observe,
        fixture.picker,
        fixture.catalog,
    )

    def assert_friendly_commands(screen):
        commands = [
            line for line in screen.splitlines() if line.lstrip().startswith(("❯ /model", "⎿"))
        ]
        assert not any("ug-auto" in line for line in commands), screen

    selector = "Choose your main model"  # The remainder wraps in a narrow native dock.
    with launch("a") as a, launch("b") as b:
        a.boot(timeout=45)
        b.boot(timeout=45)

        assert_friendly_commands(a.visible)
        before = observe(a, "before")
        assert before["usage"]["context"]["window"] == 1_000_000
        assert "ug-auto" not in picker(a).split("Select model", 1)[1]
        a.submit("/model auto")
        a.wait_for(lambda s: selector in s, "UG's main-model selector")
        a.send("\x1b[B\r", "select " + alternate_label)
        a.wait_for(
            lambda s: selector not in s and f"Auto ({alternate_label}) saved" in s,
            "the native model switch",
        )
        assert_friendly_commands(a.visible)
        changed = observe(a, "changed")
        assert changed["model"] == catalog["models"][1]["autoModel"]
        assert changed["custom"] == changed["model"]
        assert changed["usage"]["context"]["window"] == context_window
        assert changed["routing"] == "1"
        auto_screen = picker(a)
        assert "ug-auto" not in auto_screen.split("Select model", 1)[1]
        if alternate == "claude-haiku-4-5":
            assert "Effort not supported for Auto" in auto_screen
        other = observe(b, "other")
        assert other["model"] == before["model"]
        assert other["usage"]["context"]["window"] == 1_000_000
        assert other["custom"] == before["custom"]
        picker(b)
        # Reselecting Auto must still open the next step; Escape preserves the model.
        a.submit("/model")
        a.choose("Select model", "Auto")
        a.wait_for(lambda s: selector in s, "the selector after Auto reselection")
        a.send("\x1b", "cancel the selector")
        a.wait_for(lambda s: selector not in s, "the dismissed selector")
        assert_friendly_commands(a.visible)
        assert observe(a, "cancelled")["model"] == changed["model"]
        a.submit("/model")
        a.choose("Select model", "Opus 5.5")
        a.wait_for(lambda s: "Set model to" in s and selector not in s, "the ordinary model switch")
        manual = observe(a, "manual")
        assert manual["model"] == catalog["models"][0]["model"]
        assert manual["routing"] == "0"
        picker(a, checked=False)
        a.submit("/model auto")
        a.wait_for(lambda s: selector in s, "Auto enabled by /model auto")
        a.send("\x1b[A\r", "choose Opus as the Auto main model")
        a.wait_for(
            lambda s: selector not in s and "Auto (Opus 5.5) saved" in s, "the saved Auto model"
        )
        restored = observe(a, "restored")
        assert restored["model"] == catalog["models"][0]["autoModel"]
        assert restored["custom"] == restored["model"]
        assert restored["routing"] == "1"
        assert "Auto (Opus 5.5)" in a.visible
        assert_friendly_commands(a.visible)
        a.exit_normally()
        b.exit_normally()
