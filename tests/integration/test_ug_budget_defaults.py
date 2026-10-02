"""Live customer journeys for workspace smart budget defaults."""

import re
from decimal import ROUND_HALF_UP, Decimal

import pytest
from utils.budget_defaults import BudgetDefaults, BudgetTarget
from utils.evidence import (
    FileTask,
    assert_native_process,
    assert_task_model,
)
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.tui, pytest.mark.cuj5]


@pytest.fixture
def budget_defaults(live_session, workspace):
    """Supply the target; each journey opens and restores its own budget context."""
    return BudgetDefaults(live_session, workspace, BudgetTarget.from_environment())


def _assert_spend(session, text, recommendation, *, usage=False):
    """Compare displayed dollars and percentage with the independent backend response."""
    text = session.redact(text)
    money = r"\$([\d,]+\.\d{2})"
    pattern = (
        rf"Budget spend:\s*{money}\s+of\s+{money}\s+\((\d+)%\)"
        if usage
        else rf"{money}\s*/\s*{money}\s+(\d+)%\s+used"
    )
    matches = re.findall(pattern, text)
    assert matches, f"Budget spend was not displayed:\n{text}"
    actual_spend, actual_threshold, actual_percent = matches[-1]
    spend = Decimal(str(recommendation["current_spend"]))
    threshold = Decimal(str(recommendation["effective_threshold"]))
    if usage:
        # The usage command formats Decimal dollars half-up and percent with .0%.
        expected_money = [
            value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for value in (spend, threshold)
        ]
        expected_percent = int(format(float(spend / threshold), ".0%").removesuffix("%"))
    else:
        # The launch panel formats float dollars and rounds its percentage half-up.
        expected_money = [Decimal(format(float(value), ".2f")) for value in (spend, threshold)]
        expected_percent = int(float(spend) / float(threshold) * 100 + 0.5)
    assert [
        Decimal(actual_spend.replace(",", "")),
        Decimal(actual_threshold.replace(",", "")),
    ] == expected_money, text
    assert int(actual_percent) == expected_percent, text


def _assert_launch(session, tui, budget, before, label):
    assert_native_process(tui, tui.agent, label, session)
    budget.assert_stable(before, label)
    transcript = "".join(tui.output) + "\n" + tui.visible
    _assert_spend(session, transcript, before)
    text = session.redact(transcript)
    agent = before.get("recommended_agent")
    model = before.get("recommended_model")
    if agent:
        name = {"CODING_AGENT_CODEX": "Codex", "CODING_AGENT_CLAUDE_CODE": "Claude Code"}[agent]
        recommendation = f"Recommended agent is {name}"
        if model:
            recommendation += f" with model {model}"
        assert recommendation in text, text
    elif model:
        assert f"Recommended model is {model}" in text, text
    else:
        assert "Recommended agent is" not in text, text
        assert "Recommended model is" not in text, text


def test_bare_ug_launches_claude_below_50_percent(live_session, budget_defaults):
    """Scenario: configure the workspace and launch bare ``ug`` at 40% budget usage.

    Expected: Claude completes a file task on Sonnet; its real process and the
    usage/launch displays agree with the stable backend budget response.
    """
    session = live_session
    label = "bare-40-percent"
    with budget_defaults as budget:
        session.run(
            "configure",
            "--workspace",
            budget.workspace,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        before = budget.prepare(Decimal("2.5"), label)
        assert before.get("recommended_agent") in (None, "CODING_AGENT_CLAUDE_CODE"), before
        assert before.get("recommended_model") in (None, budget.sonnet_model), before
        usage = session.run("usage", timeout=120)
        _assert_spend(session, usage.stdout + usage.stderr, before, usage=True)

        task = FileTask(session)
        with AgentTerminal(session, "claude", [str(session.binary)], label) as tui:
            tui.boot(timeout=240)
            _assert_launch(session, tui, budget, before, label)
            tui.submit(task.prompt)
            tui.wait_for_task(task, timeout=240)
            assert_task_model(session, "claude", task, budget.sonnet_model, label)
            tui.exit_normally()
        session.assert_not_routed()


@pytest.mark.parametrize(
    ("multiplier", "model_attribute"),
    [
        pytest.param("2", "sol_model", id="exact-50"),
        pytest.param("1.6", "sol_model", id="between-50-and-80"),
        pytest.param("1.25", "luna_model", id="exact-80"),
        pytest.param("1.125", "luna_model", id="between-80-and-100"),
    ],
)
def test_bare_ug_launches_codex_at_budget_tier(
    live_session, budget_defaults, multiplier, model_attribute
):
    """Scenario: launch bare ``ug`` at 50%, 62.5%, 80%, or approximately 88.9% usage.

    Expected: the highest applicable tier selects Codex Sol or Luna, superseding
    Codex's Sol default at 80%+. Check the backend response before launch, the native
    process, usage display, and completed task's model. Unstable exact boundaries fail.
    """
    session = live_session
    label = f"bare-codex-{multiplier}"
    with budget_defaults as budget:
        session.run(
            "configure",
            "--workspace",
            budget.workspace,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        model = getattr(budget, model_attribute)
        before = budget.prepare(Decimal(multiplier), label)
        assert before.get("recommended_agent") == "CODING_AGENT_CODEX", before
        assert before.get("recommended_model") == model, before
        usage = session.run("usage", timeout=120)
        _assert_spend(session, usage.stdout + usage.stderr, before, usage=True)

        task = FileTask(session)
        with AgentTerminal(session, "codex", [str(session.binary)], label) as tui:
            tui.boot(timeout=240)
            _assert_launch(session, tui, budget, before, label)
            tui.submit(task.prompt)
            tui.wait_for_task(task, timeout=240)
            assert_task_model(session, "codex", task, model, label)
            tui.exit_normally()
        session.assert_not_routed()


def test_ug_claude_keeps_its_default_when_budget_recommends_codex(live_session, budget_defaults):
    """Scenario: run explicit ``ug claude`` at 80%, where the budget recommends Codex Luna.

    Expected: the real Claude TUI displays that recommendation but completes a file
    task on its own Sonnet default. The Codex-only Luna model never reaches Claude.
    """
    session = live_session
    label = "explicit-claude-luna-tier"
    with budget_defaults as budget:
        session.run(
            "configure",
            "--workspace",
            budget.workspace,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        before = budget.prepare(Decimal("1.25"), label)
        assert before.get("recommended_agent") == "CODING_AGENT_CODEX", before
        assert before.get("recommended_model") == budget.luna_model, before
        usage = session.run("usage", timeout=120)
        _assert_spend(session, usage.stdout + usage.stderr, before, usage=True)

        task = FileTask(session)
        with AgentTerminal(session, "claude", [str(session.binary), "claude"], label) as tui:
            tui.boot(timeout=240)
            _assert_launch(session, tui, budget, before, label)
            tui.submit(task.prompt)
            tui.wait_for_task(task, timeout=240)
            assert_task_model(session, "claude", task, budget.sonnet_model, label)
            tui.exit_normally()
        session.assert_not_routed()
