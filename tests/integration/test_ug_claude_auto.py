"""Native Auto picker journey, including real routing on and off."""

import json
import re
import shutil
from urllib.parse import urlparse

import pytest
from utils.evidence import (
    FileTask,
    SubagentCalculation,
    assert_completed_task_model,
    assert_subagent_routed,
    read_jsonl,
)
from utils.managed import use_managed_config_fixture
from utils.terminal import AgentTerminal

pytestmark = [
    pytest.mark.live,
    pytest.mark.tui,
    pytest.mark.claude,
    pytest.mark.managed_fixture,
    pytest.mark.harness_latest,
]


def test_ug_claude_auto_picker_routes_then_manual_model_disables_routing(live_session, workspace):
    """Scenario: configure a managed catalog, choose Auto in the stock 80-column
    /model picker, customize its main model, run a child, then select a regular model.

    Expected: all permitted main models appear, Escape preserves the preference,
    reselecting Auto opens the selector, and /auto alone does not enable routing.
    Both child tasks and parent answers complete; only the Auto task has a correlated
    gateway decision. The regular model has no Auto indicator. This checks settled
    UI states, not individual rendered frames. Fresh launches preserve saved native
    choices; resume matches Claude's native model/settings precedence.
    """
    session = live_session
    # The staging workspace exposes the 5.5 generation; the CI workspace uses
    # the existing routing fixture. The caller still selects workspace/auth.
    fixture = (
        "claude_auto"
        if urlparse(workspace).hostname
        == "eng-ml-agent-bricks-us-east-1.staging.cloud.databricks.com"
        else "claude_smart_routing"
    )
    path = use_managed_config_fixture(session, fixture)
    config = json.loads(path.read_text())["enabled_agents"][0]["config"]
    models = config["models"]["model_services"]
    labels = [
        re.sub(r"(?<=\d)-(?=\d)", ".", m.removeprefix("system.ai.claude-"))
        .replace("-", " ")
        .title()
        for m in models
    ]
    session.run(
        "configure",
        "--agent",
        "claude",
        "--workspace",
        workspace,
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
        timeout=240,
    )
    selector = "Choose your main model for Smart Routing"
    decisions = session.home / ".ucode/claude-smart-routing-decisions.jsonl"

    # A user-owned observer reports native state without changing model or routing.
    observer = session.cwd / "auto-readiness-observer"
    (observer / ".claude-plugin").mkdir(parents=True)
    (observer / "hooks").mkdir()
    (observer / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "auto-readiness", "version": "1.0.0"})
    )
    (observer / "hooks/hooks.json").write_text('{"modules":["./register.ts"]}')
    (observer / "hooks/register.ts").write_text("""
export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.command.register({name:'auto-state', description:'Observe native state', immediate:true});
    return next(e);
  });
  on('command.run', {command:'auto-state'}, async ($, e) => {
    await $.fs.write('state-' + e.args + '.json', JSON.stringify({
      id:await $.session.id(), model:await $.session.model(), usage:await $.session.usage(),
      routing:await $.env.get('ENABLE_SMART_ROUTING_SUBAGENT_ONLY')
    }));
    return {text:'STATE_RECORDED_' + e.args};
  });
}
""")

    def snapshot(tui, label):
        tui.submit("/auto-state " + label)
        path = session.cwd / ("state-" + label + ".json")
        tui.wait_for(lambda _: path.exists(), "native state for " + label)
        return json.loads(path.read_text())

    launch = [str(session.binary), "claude", "--", "--plugin-dir", str(observer)]

    with AgentTerminal(session, "claude", launch, "auto-picker", dimensions=(60, 80)) as tui:
        tui.boot()
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        tui.wait_for(
            lambda s: selector in s and all(label in s for label in labels),
            "the complete permitted main-model list",
        )
        tui.send("\x1b[B\r", "choose the second main model")
        tui.wait_for(
            lambda s: selector not in s and f"Auto ({labels[1]}) saved" in s, "the saved main model"
        )

        tui.submit("/auto")
        tui.wait_for(lambda s: selector in s, "the /auto selector")
        tui.send("\x1b", "cancel without changing the main model")
        tui.wait_for(lambda s: selector not in s, "the dismissed selector")
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        tui.wait_for(lambda s: selector in s, "the selector after reselecting Auto")
        tui.send("\x1b", "keep the previously chosen main model")
        tui.wait_for(lambda s: selector not in s, "the dismissed selector")

        routed = SubagentCalculation("19*23", "437")
        tui.submit(routed.prompt)
        tui.wait_for_task(routed, timeout=180)
        tui.wait_for(
            lambda _: routed.completed(session, "claude", child=True),
            "the routed child's answer",
            timeout=180,
        )
        routed.assert_completed(session, "claude", child=True)
        assert_completed_task_model(session, "claude", routed.value, models[1])
        assert_subagent_routed(session, "claude", routed)
        tui.wait_for_background_tasks()

        tui.submit("/model")
        tui.choose("Select model", labels[1])
        tui.wait_for(
            lambda s: "Set model to" in s and selector not in s, "the regular model selection"
        )
        # /auto may save a preference while a regular model remains selected.
        tui.submit("/auto")
        tui.wait_for(lambda s: selector in s, "the preference-only selector")
        tui.send("\x1b[A\r", "save the first main model without enabling Auto")
        tui.wait_for(
            lambda s: selector not in s and f"Auto ({labels[0]}) saved" in s,
            "the preference confirmation",
        )
        before = read_jsonl(decisions)
        manual = SubagentCalculation("17*29", "493")
        tui.submit(manual.prompt)
        tui.wait_for_task(manual, timeout=180)
        tui.wait_for(
            lambda _: manual.completed(session, "claude", child=True),
            "the unrouted child's answer",
            timeout=180,
        )
        manual.assert_completed(session, "claude", child=True)
        assert_completed_task_model(session, "claude", manual.value, models[1])
        assert read_jsonl(decisions) == before
        # Toasts/transcript confirmations may say Auto; only the footer is checked.
        assert "Auto (" not in "\n".join(tui.visible.splitlines()[-4:])
        tui.wait_for_background_tasks()
        original = snapshot(tui, "original")
        tui.exit_normally()

    with AgentTerminal(session, "claude", launch, "auto-restart") as tui:
        tui.boot()
        restarted = snapshot(tui, "restart")
        assert restarted["model"] == original["model"]
        assert restarted["routing"] == "0"
        tui.submit("/model opus")
        tui.wait_for(
            lambda screen: "Set model to" in screen and selector not in screen,
            "the saved native alias",
        )
        alias = snapshot(tui, "alias")
        tui.exit_normally()

    with AgentTerminal(session, "claude", launch, "auto-restart-alias") as tui:
        tui.boot()
        assert snapshot(tui, "restart-alias")["model"] == alias["model"]
        tui.exit_normally()

    # Compare resume to the same installed Claude with the same managed settings.
    # ANTHROPIC_MODEL, when configured, legitimately outranks a restored model.
    native = shutil.which("claude", path=session.env["PATH"])
    assert native is not None
    resume = ["--resume", original["id"], "--plugin-dir", str(observer)]

    def resumed_prompt(tui):
        # Restored history can scroll the startup title out of the viewport.
        # Require the actual empty input and native footer, then execute the observer.
        tui.wait_for(
            lambda screen: re.search(
                r"(?m)^[ \t]*❯[ \t]*\n[─━]+\n[^\n]*(?:mode on|for shortcuts)", screen
            ),
            "the resumed native input prompt",
        )

    with AgentTerminal(session, "claude", [native, *resume], "auto-native-resume") as tui:
        resumed_prompt(tui)
        native_resumed = snapshot(tui, "native-resume")
        tui.exit_normally()
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude", "--", *resume], "auto-ug-resume"
    ) as tui:
        resumed_prompt(tui)
        resumed = snapshot(tui, "ug-resume")
        assert resumed["model"] == native_resumed["model"]
        assert resumed["usage"]["context"]["window"] == native_resumed["usage"]["context"]["window"]
        task = FileTask(session)
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        task.assert_completed(session, "claude")
        tui.exit_normally()
