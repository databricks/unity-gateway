"""Real terminal/profile checks; no gateway inference or model responses."""

import json
import re

import pytest
from auto_session import auto_session


@pytest.mark.parametrize("family", ["opus", "sonnet"])
def test_auto_entry_matches_native_million_context_picker_variant(native_binary, tmp_path, family):
    """Scenario: native /model adds [1m] while Auto's immutable targets remain bare.

    Expected: each native 5.5 alias and explicit [1m] picker model reports 1M;
    returning to Auto selects the same family before its chooser, and Escape
    preserves that Auto profile. No inference runs, so warm-cache behavior is
    checked by the live integration recorder rather than this metadata test.
    """
    models = ["system.ai.claude-opus-5-5", "system.ai.claude-sonnet-5-5"]
    fixture = auto_session(
        native_binary,
        tmp_path,
        models,
        dict(zip(models, ["Opus 5.5", "Sonnet 5.5"], strict=True)),
    )
    row = next(row for row in fixture.catalog["models"] if family in row["model"])
    # Ordinary native settings input reproduces UG's [1m] picker hints, while
    # retaining the bare alias targets from the managed Auto catalog.
    path = tmp_path / "settings.json"
    settings = json.loads(path.read_text())
    for option in settings["modelPicker"]["options"]:
        option["model"] += "[1m]"
        settings["availableModels"].append(option["model"])
    path.write_text(json.dumps(settings))
    fixture.catalog["model"] = row["autoModel"]
    selector = "Choose your main model"
    with fixture.launch("native-context") as tui:
        tui.boot(timeout=45)
        original = fixture.observe(tui, "original")
        assert original["model"] == row["autoModel"]
        assert original["usage"]["context"]["window"] == 1_000_000
        tui.submit("/model")
        tui.choose("Select model", row["label"])
        tui.wait_for(lambda s: "smart router OFF" in s, "the manual native model")
        manual = fixture.observe(tui, "manual")
        assert manual["model"] == row["model"] + "[1m]"
        assert manual["usage"]["context"]["window"] == 1_000_000
        assert manual["routing"] == "0"
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        tui.wait_for(
            lambda s: selector in s and f"❯ {row['label']}" in s,
            "the same-family Auto main chooser",
        )
        assert "Switch model?" not in tui.visible
        assert "blocked by a PreModelSwitch hook" not in tui.visible
        tui.send("\x1b", "keep the Auto profile selected at entry")
        tui.wait_for(lambda s: selector not in s, "the dismissed main chooser")
        selected = fixture.observe(tui, "selected")
        assert selected["model"] == row["autoModel"]
        assert selected["custom"] == row["autoModel"]
        assert selected["usage"]["context"]["window"] == 1_000_000
        assert selected["routing"] == "1"
        tui.exit_normally()


@pytest.mark.parametrize("policy_decision", ["deny", "ask"])
def test_auto_policy_denial_restores_profile_and_allows_retry(
    native_binary, tmp_path, policy_decision
):
    """Scenario: a native policy denies a model or asks for confirmation, then the user cancels.

    Expected: native denial restores the current Auto pointer, saves no different
    profile, and permits the same selection after the policy is removed.
    No gateway inference runs.
    """
    fixture = auto_session(
        native_binary,
        tmp_path,
        ["claude-opus-5-5", "claude-sonnet-5-5"],
        {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5"},
    )
    target = fixture.catalog["models"][1]["autoModel"]
    policy = tmp_path / "project" / "deny-switch"
    policy.write_text("Deny the Sonnet selection")
    observer = tmp_path / "observer" / "hooks" / "register.ts"
    observer.write_text(
        observer.read_text().replace(
            "export function register(on) {",
            "export function register(on) {"
            + """
  on('classic.PreModelSwitch', {to_model:AUTO_TARGET}, async ($, e, next) => {
    const result = await next(e);
    return await $.fs.exists('deny-switch')
      ? {...result, permissionDecision:POLICY_DECISION, permissionDecisionReason:'Test model policy'}
      : result;
  });
""".replace("AUTO_TARGET", json.dumps(target)).replace(
                "POLICY_DECISION", json.dumps(policy_decision)
            ),
        )
    )
    selector = "Choose your main model"
    with fixture.launch("policy") as tui:
        tui.boot(timeout=45)
        tui.submit("/model auto")
        tui.wait_for(lambda s: selector in s, "the main-model chooser")
        tui.send("\x1b[B\r", "choose the model denied by policy")
        if policy_decision == "ask":
            tui.wait_for(
                lambda s: "Switch model?" in s and "Test model policy" in s,
                "native policy confirmation after the main choice",
            )
            assert selector not in tui.visible
            tui.send("\x1b[B\r", "cancel the native policy confirmation")
        tui.wait_for(
            lambda s: selector in s and "Switch model?" not in s,
            "the reopened chooser after cancellation or denial",
        )
        if policy_decision == "deny":
            assert "blocked by a PreModelSwitch hook" in tui.visible
        assert "Could not save Auto" not in tui.visible
        tui.send("\x1b", "dismiss the reopened chooser")
        tui.wait_for(lambda s: selector not in s, "the dismissed chooser")
        denied = fixture.observe(tui, "denied")
        assert denied["model"] == fixture.catalog["models"][0]["autoModel"]
        assert denied["custom"] == denied["model"]
        assert denied["routing"] == "1"
        policy.unlink()
        tui.submit("/model auto")
        tui.wait_for(lambda s: selector in s, "retrying the main-model choice")
        tui.send("\x1b[B\r", "choose Sonnet after the policy is removed")
        tui.wait_for(
            lambda s: selector not in s and "Auto (Sonnet 5.5) saved" in s,
            "the accepted retry",
        )
        accepted = fixture.observe(tui, "accepted")
        assert accepted["model"] == target
        assert accepted["custom"] == target
        assert accepted["routing"] == "1"
        tui.exit_normally()


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
    Model-command echoes and confirmations use friendly Auto names. The band and
    footer show ON for Auto and OFF for manual models.
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

    def assert_routing_status(tui, *, enabled, label=None):
        state = "ON" if enabled else "OFF"
        tui.wait_for(
            lambda s: (
                re.search(rf"(?m)^\s*unity gateway smart router\s+{state}\b", s)
                and f"smart router {state}" in "\n".join(s.splitlines()[-4:])
                and (label is None or f"Auto ({label})" in s)
            ),
            f"the Smart Router {state} band and footer",
        )

    selector = "Choose your main model"  # The remainder wraps in a narrow native dock.
    with launch("a") as a, launch("b") as b:
        a.boot(timeout=45)
        b.boot(timeout=45)

        assert_routing_status(a, enabled=True, label="Opus 5.5")
        assert_routing_status(b, enabled=True, label="Opus 5.5")
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
        assert_routing_status(a, enabled=True, label=alternate_label)
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
        assert_routing_status(a, enabled=False)
        manual = observe(a, "manual")
        assert manual["model"] == catalog["models"][0]["model"]
        assert manual["routing"] == "0"
        picker(a, checked=False)
        a.submit("/model auto")
        a.wait_for(
            lambda s: selector in s and "❯ Opus 5.5" in s,
            "Auto retains the current Opus main model",
        )
        a.send("\r", "keep Opus as the Auto main model")
        a.wait_for(
            lambda s: selector not in s and "Auto (Opus 5.5) saved" in s, "the saved Auto model"
        )
        assert_routing_status(a, enabled=True, label="Opus 5.5")
        restored = observe(a, "restored")
        assert restored["model"] == catalog["models"][0]["autoModel"]
        assert restored["custom"] == restored["model"]
        assert restored["routing"] == "1"
        assert "Auto (Opus 5.5)" in a.visible
        assert_friendly_commands(a.visible)
        a.exit_normally()
        b.exit_normally()


def test_auto_feedback_escape_preserves_exact_auto_and_routing(native_binary, tmp_path):
    """Scenario: select a manual model from a nondefault Auto profile with feedback on.

    Expected: the existing feedback pane opens after native selection, Escape
    preserves the exact Auto alias and routing ON, and another picker still works.
    Only the external Isaac target collector is a fixture. Claude and the form
    remain real; this never submits feedback or inference.
    """
    fixture = auto_session(
        native_binary,
        tmp_path,
        ["claude-opus-5-5", "claude-haiku-4-5"],
        {"claude-opus-5-5": "Opus 5.5", "claude-haiku-4-5": "Haiku 4.5"},
        feedback=True,
    )
    selector = "Choose your main model"
    with fixture.launch("feedback") as tui:
        tui.boot(timeout=45)
        tui.submit("/model auto")
        tui.wait_for(lambda s: selector in s, "the Auto selector")
        pane = " ".join(line.partition("│")[2].strip() for line in tui.visible.splitlines())
        assert "Subagents are smart routed. Your main agent isn't; choose its model below." in (
            " ".join(pane.split())
        )
        tui.send("\x1b[B\r", "choose Haiku as Auto's main model")
        tui.wait_for(lambda s: selector not in s and "Auto (Haiku 4.5) saved" in s, "saved Auto")
        expected = fixture.catalog["models"][1]["autoModel"]
        assert fixture.observe(tui, "before-feedback")["model"] == expected
        for attempt in range(2):
            tui.submit("/model")
            tui.choose("Select model", "Opus 5.5")
            tui.wait_for(
                lambda s: "Why are you turning Smart Router off?" in s,
                "the existing feedback form after native manual selection",
            )
            assert selector not in tui.visible
            assert "Enter at least 16 characters." in tui.visible
            assert (tui.session.cwd / "feedback-inspected").is_file()
            tui.send("\x1b", "cancel feedback without a reason or upload")
            tui.wait_for(
                lambda s: "Why are you turning Smart Router off?" not in s,
                "the dismissed feedback form",
            )
            restored = fixture.observe(tui, f"cancelled-feedback-{attempt}")
            assert restored["model"] == expected
            assert restored["routing"] == "1"
            assert "smart router ON" in tui.visible
        fixture.picker(tui)
        tui.submit("/model auto")
        tui.wait_for(lambda s: selector in s, "the ordinary Auto selector after feedback")
        assert "Why are you turning Smart Router off?" not in tui.visible
        tui.send("\x1b", "close the restored Auto selector")
        tui.wait_for(lambda s: selector not in s, "the dismissed Auto selector")
        restored = fixture.observe(tui, "after-feedback-selector")
        assert restored["model"] == expected
        assert restored["routing"] == "1"
        tui.exit_normally()
