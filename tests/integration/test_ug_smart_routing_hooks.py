"""CUJs for subagent-only routing against the live workspace router.

The agent harness invokes ``ug claude-router-hook route-subagent`` /
``ug codex-router-hook route-subagent`` on its PreToolUse event with a JSON payload on
stdin. These journeys drive the real installed hook commands through that stdin contract,
so the routing decision, response shape, and audit trail are asserted without relying on
an agent choosing to spawn a subagent. The TUI journeys additionally invoke the installed
Smart Router skill and spawn real children before and after its session-local toggles.
"""

import json

import pytest
from utils.constants import CLAUDE_SMART_ROUTING_MODELS, CODEX_SMART_ROUTING_MODELS
from utils.evidence import (
    SubagentCalculation,
    assert_subagent_routed,
    assistant_answer_contains,
    read_jsonl,
)
from utils.managed import (
    build_claude_agent_config,
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.terminal import AgentTerminal

SMART_ROUTING_BANNER = "Using Unity Gateway Smart Router."
SMART_ROUTING_SUBAGENT_NOTICE = "Using Unity Gateway Smart Router - Subagent"
CLAUDE_MODELS = [
    "system.ai.claude-opus-5",
    "system.ai.claude-sonnet-5",
    "system.ai.claude-haiku-4-5",
    "system.ai.glm-5-3",
    "system.ai.kimi-k3",
]
CODEX_MODELS = [
    "system.ai.gpt-6-astra",
    "system.ai.gpt-5-6-sol",
    "system.ai.gpt-5-6-terra",
    "system.ai.gpt-5-6-luna",
    "system.ai.gpt-5-5",
    "system.ai.glm-5-3",
    "system.ai.kimi-k3",
]
# Codex launches subagents on its bundled catalog slugs, not the workspace model id, so
# the routed model in the hook response must be one of these slugs.
CODEX_MODEL_SLUGS = {
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "glm-5-3",
    "kimi-k3",
}
SKILL_ROOTS = {"claude": ".claude/skills", "codex": ".codex/skills"}


def _routing_decisions(session, agent: str) -> list[dict]:
    return read_jsonl(session.home / ".ucode" / f"{agent}-smart-routing-decisions.jsonl")


def _routing_banner_for_task(screen: str, marker: str) -> bool:
    """Whether the rendered router panel belongs to this uniquely tagged task."""
    lines = screen.splitlines()
    for index, line in enumerate(lines):
        if SMART_ROUTING_SUBAGENT_NOTICE not in line:
            continue
        panel = []
        for panel_line in lines[index : index + 12]:
            panel.append(panel_line)
            if "└" in panel_line:
                break
        # Rich can wrap the marker between any two characters in a narrow TUI.
        # Compare without rendered whitespace so the banner remains attributable.
        if marker in "".join("\n".join(panel).split()):
            return True
    return False


def _run_calculation(tui, session, agent: str, expression: str, expected: str, *, routed: bool):
    task = SubagentCalculation(expression, expected)
    before = _routing_decisions(session, agent)
    tui.submit(task.prompt)

    if routed:
        tui.wait_for(
            lambda screen: _routing_banner_for_task(screen, task.marker),
            f"the Smart Router subagent banner for {task.marker}",
            timeout=120,
        )
    tui.wait_for_task(task, timeout=180)
    task.assert_completed(session, agent)
    task.assert_completed(session, agent, child=True)

    after = _routing_decisions(session, agent)
    new_decisions = after[len(before) :]
    if not routed:
        assert not _routing_banner_for_task(tui.visible, task.marker), tui.visible
        assert not new_decisions, new_decisions
        return

    assert len(new_decisions) == 1, new_decisions
    decision = new_decisions[0]
    assert task.marker in decision.get("task_name", ""), decision
    assert_subagent_routed(
        session,
        agent,
        task,
        decision_ids={decision["decision_id"]},
    )


def _toggle_with_skill(tui, session, agent: str, enabled: bool) -> None:
    skill_root = session.home / SKILL_ROOTS[agent]
    ignored_skills = {".system"} if agent == "codex" else set()
    installed_skills = sorted(
        path.name
        for path in skill_root.iterdir()
        if path.is_dir() and path.name not in ignored_skills
    )
    assert installed_skills == ["smart-router"], installed_skills

    state = "on" if enabled else "off"
    invocation = f"/smart-router {state}" if agent == "claude" else f"$smart-router {state}"
    confirmation = f"{state} for this session"
    tui.submit(invocation)
    tui.wait_for(
        lambda screen: (
            "Smart Router" in screen
            and confirmation in screen
            and (
                assistant_answer_contains(session, agent, confirmation)
                or assistant_answer_contains(session, agent, f"**{state}** for this session")
            )
        ),
        f"the installed Smart Router skill to turn routing {state}",
        timeout=120,
    )


@pytest.mark.live
@pytest.mark.claude
def test_smart_routing_claude_route_subagent_hook(live_session, workspace):
    """Scenario: with subagent-only routing enabled, Claude Code fires PreToolUse for an
    Agent spawn, piping the payload to ``ug claude-router-hook route-subagent``.

    Expected: the hook allows the call against the real workspace router, drops the
    requested model in favor of a ``ucode-route-`` agent definition while preserving the
    task text, and audits one decision naming an offered model for the session. Only the
    hook contract is asserted; no agent decides to spawn.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    payload = {
        "session_id": "claude-route-subagent-hook",
        "tool_name": "Agent",
        "tool_input": {
            "description": "Refactor the parser",
            "prompt": "Refactor the parser module into a package and add unit tests.",
            "subagent_type": "general-purpose",
            "model": "sonnet",
        },
    }
    result = session.run(
        "claude-router-hook",
        "route-subagent",
        "--host",
        workspace,
        *(arg for model in CLAUDE_MODELS for arg in ("--model", model)),
        input_text=json.dumps(payload),
        timeout=60,
    )
    output = json.loads(result.stdout)
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse", output
    assert hook["permissionDecision"] == "allow", output
    assert SMART_ROUTING_SUBAGENT_NOTICE in output["systemMessage"], output
    updated = hook["updatedInput"]
    assert "model" not in updated, updated
    assert updated["subagent_type"].startswith("ug-smart-router:ucode-route-"), updated
    assert updated["prompt"] == payload["tool_input"]["prompt"], updated
    assert updated["description"] == payload["tool_input"]["description"], updated

    decisions_path = session.home / ".ucode" / "claude-smart-routing-decisions.jsonl"
    assert decisions_path.is_file(), f"hook wrote no routing decision record: {decisions_path}"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    session.record("claude-smart-routing-decisions.jsonl", rows)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["session_id"] == payload["session_id"], row
    assert row["task_name"] == payload["tool_input"]["prompt"], row
    assert row["requested_model"] in CLAUDE_MODELS, row


@pytest.mark.live
@pytest.mark.codex
def test_smart_routing_codex_route_subagent_hook(live_session, workspace):
    """Scenario: with subagent-only routing enabled, Codex fires PreToolUse for a
    spawn_agent call, piping the payload to ``ug codex-router-hook route-subagent``.

    Expected: the hook allows the call against the real workspace router, rewrites the
    requested model to the bundled catalog slug of an offered model while preserving the
    task message, and audits one decision matching the response for the session. Only the
    hook contract is asserted; no agent decides to spawn.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    payload = {
        "session_id": "codex-route-subagent-hook",
        "tool_name": "spawn_agent",
        "tool_input": {
            "task_name": "Refactor the parser",
            "message": "Refactor the parser module into a package and add unit tests.",
            "model": "gpt-5.5",
        },
    }
    result = session.run(
        "codex-router-hook",
        "route-subagent",
        "--host",
        workspace,
        *(arg for model in CODEX_MODELS for arg in ("--model", model)),
        input_text=json.dumps(payload),
        timeout=60,
    )
    output = json.loads(result.stdout)
    hook = output["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse", output
    assert hook["permissionDecision"] == "allow", output
    assert SMART_ROUTING_SUBAGENT_NOTICE in output["systemMessage"], output
    updated = hook["updatedInput"]
    assert updated["model"] in CODEX_MODEL_SLUGS, updated
    assert updated["message"] == payload["tool_input"]["message"], updated
    assert updated["task_name"] == payload["tool_input"]["task_name"], updated

    decisions_path = session.home / ".ucode" / "codex-smart-routing-decisions.jsonl"
    assert decisions_path.is_file(), f"hook wrote no routing decision record: {decisions_path}"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    session.record("codex-smart-routing-decisions.jsonl", rows)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["session_id"] == payload["session_id"], row
    assert row["task_name"] == payload["tool_input"]["message"], row
    assert row["requested_model"] == updated["model"], row


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.managed_fixture
def test_smart_router_skill_toggles_claude_subagent_routing(live_session, workspace, tmp_path):
    """Scenario: launch Claude with subagent routing enabled, spawn a child, invoke the
    installed Smart Router skill to turn routing off, spawn another child, turn routing
    back on through the skill, and spawn a third child in the same real TUI session.

    Expected: Smart Router is the only user-installed Claude skill; all three uniquely tagged
    calculations complete in native child sessions; only the first and third show the
    subagent-routing banner and produce live gateway decisions correlated with those children.
    No first-prompt routing wrapper starts.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    config = build_coding_agent_config(
        "CODING_AGENT_CLAUDE_CODE",
        build_claude_agent_config(CLAUDE_SMART_ROUTING_MODELS, smart_routing=True),
    )
    set_managed_config_stub(session, tmp_path, config)
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "smart-router-skill-toggle"
    ) as tui:
        tui.boot()
        _run_calculation(tui, session, "claude", "1+1", "2", routed=True)
        _toggle_with_skill(tui, session, "claude", enabled=False)
        _run_calculation(tui, session, "claude", "1+2", "3", routed=False)
        _toggle_with_skill(tui, session, "claude", enabled=True)
        _run_calculation(tui, session, "claude", "2+2", "4", routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
    canary = session.home / ".ucode" / "claude-smart-routing-canary.json"
    assert canary.is_file(), f"routing hooks were not armed: {canary}"


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
def test_smart_router_skill_toggles_codex_subagent_routing(live_session, workspace, tmp_path):
    """Scenario: launch Codex with subagent routing enabled, spawn a child, invoke the
    installed Smart Router skill to turn routing off, spawn another child, turn routing
    back on through the skill, and spawn a third child in the same real TUI session.

    Expected: Smart Router is the only user-installed Codex skill; all three uniquely tagged
    calculations complete in native child sessions; only the first and third show the
    subagent-routing banner and produce live gateway decisions correlated with those children.
    No first-prompt interposer starts.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_V2"] = "1"
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(models=CODEX_SMART_ROUTING_MODELS, smart_routing=True),
    )
    set_managed_config_stub(session, tmp_path, config)
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "smart-router-skill-toggle"
    ) as tui:
        tui.boot()
        _run_calculation(tui, session, "codex", "1+1", "2", routed=True)
        _toggle_with_skill(tui, session, "codex", enabled=False)
        _run_calculation(tui, session, "codex", "1+2", "3", routed=False)
        _toggle_with_skill(tui, session, "codex", enabled=True)
        _run_calculation(tui, session, "codex", "2+2", "4", routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
