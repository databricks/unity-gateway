"""Native Auto picker journey, including real routing on and off."""

import json
import re
from urllib.parse import urlparse

import pytest
from utils.evidence import (
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
    UI states, not absence of the known transient pane flash or dynamic capabilities.
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
    selector = "Choose your main model"
    decisions = session.home / ".ucode/claude-smart-routing-decisions.jsonl"

    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "auto-picker", dimensions=(60, 80)
    ) as tui:
        tui.boot()
        tui.submit("/model")
        tui.choose("Select model", "Auto")
        tui.wait_for(
            lambda s: selector in s and all(label in s for label in labels),
            "the complete permitted main-model list",
        )
        tui.send("\x1b[B\r", "choose the second main model")
        tui.wait_for(
            lambda s: selector not in s and f"Auto({labels[1]}) saved" in s, "the saved main model"
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
            lambda s: selector not in s and f"Auto({labels[0]}) saved" in s,
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
        assert "Auto(" not in "\n".join(tui.visible.splitlines()[-4:])
        tui.wait_for_background_tasks()
        tui.exit_normally()
