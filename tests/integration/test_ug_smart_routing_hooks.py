"""CUJs for subagent-only routing against the live workspace router.

The agent harness invokes ``ug claude-router-hook route-subagent`` /
``ug codex-router-hook route-subagent`` on its PreToolUse event with a JSON payload on
stdin. These journeys drive the real installed hook commands through that stdin contract,
so the routing decision, response shape, and audit trail are asserted without relying on
an agent choosing to spawn a subagent. The TUI journeys additionally invoke the installed
Smart Router skill and spawn real children before and after its session-local toggles.
"""

import json
from pathlib import Path

import pytest
from utils.evidence import (
    SubagentCalculation,
    agent_sessions,
    assert_subagent_routed,
    assistant_answers,
    is_child_session,
    read_jsonl,
    tool_outputs,
)
from utils.managed import use_managed_config_fixture
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


def _toggle_with_skill(
    tui, session, agent: str, enabled: bool, orchestration_enabled: bool
) -> None:
    skill_root = session.home / SKILL_ROOTS[agent]
    ignored_skills = {".system"} if agent == "codex" else set()
    installed_skills = sorted(
        path.name
        for path in skill_root.iterdir()
        if path.is_dir() and path.name not in ignored_skills
    )
    expected_skills = (
        ["smart-router", "smart-router-orchestrator"] if orchestration_enabled else ["smart-router"]
    )
    assert installed_skills == expected_skills, installed_skills

    state = "on" if enabled else "off"
    invocation = f"/smart-router {state}" if agent == "claude" else f"$smart-router {state}"
    controls = list(Path(session.env["TMPDIR"]).glob("ug-session-env-*/env.json"))
    assert len(controls) == 1, controls
    expected = (
        {}
        if enabled
        else {
            "ENABLE_SMART_ROUTING_V2": "0",
            "ENABLE_SMART_ROUTING_SUBAGENT_ONLY": "0",
            "ENABLE_SMART_ROUTER_ORCHESTRATOR": "0",
        }
    )
    assert json.loads(controls[0].read_text()) != expected

    confirmation = f"Smart Router is {state} for this session"

    def completion_counts():
        answers = confirmations = 0
        for path, records in agent_sessions(session, agent).items():
            if is_child_session(agent, path, records):
                continue
            answers += len(assistant_answers(agent, records))
            confirmations += sum(confirmation in output for output in tool_outputs(agent, records))
        return answers, confirmations

    before_answers, before_confirmations = completion_counts()
    tui.submit(invocation)

    def toggled(_screen):
        answers, confirmations = completion_counts()
        return (
            json.loads(controls[0].read_text()) == expected
            and confirmations > before_confirmations
            and answers > before_answers
        )

    tui.wait_for(
        toggled,
        f"the installed Smart Router skill to turn routing {state}",
        timeout=120,
    )


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "legacy-env",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_routing_claude_route_subagent_hook(
    live_session, workspace, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: legacy subagent routing or each supported selector makes Claude Code fire
    PreToolUse for an Agent spawn, piping the payload to ``ug claude-router-hook route-subagent``.

    Expected: the hook allows the call against the real workspace router, drops the
    requested model in favor of a ``ucode-route-`` agent definition while preserving the
    task text, and audits one decision naming an offered model for the session. Only the
    hook contract is asserted; no agent decides to spawn.
    """
    session = live_session
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    else:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
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
@pytest.mark.parametrize(
    "SMART_ROUTER_CONFIG_VERSION",
    [
        None,
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
    ids=[
        "legacy-env",
        "first_prompt_and_subagent_no_orch_v0",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_routing_codex_route_subagent_hook(
    live_session, workspace, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: legacy subagent routing or each supported selector makes Codex fire PreToolUse
    for a spawn_agent call, piping the payload to ``ug codex-router-hook route-subagent``.

    Expected: the hook allows the call against the real workspace router, rewrites the
    requested model to the bundled catalog slug of an offered model while preserving the
    task message, and audits one decision matching the response for the session. Only the
    hook contract is asserted; no agent decides to spawn.
    """
    session = live_session
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    else:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
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
@pytest.mark.parametrize(
    "orchestration_enabled, SMART_ROUTER_CONFIG_VERSION",
    [
        (False, None),
        (True, None),
        (False, "subagent_only_v0"),
        (False, "subagent_only_v1"),
        (True, "subagent_orch_v0"),
    ],
    ids=[
        "routing-only",
        "orchestration",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_router_skill_toggles_claude_subagent_routing(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Claude with the legacy flags or a subagent selector, spawn a child, invoke
    the installed Smart Router skill to turn routing off, spawn another child, turn routing
    back on through the skill, and spawn a third child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each invocation records the CLI
    confirmation in the native transcript and changes the saved routing controls, even with
    collapsed terminal output; all three uniquely tagged calculations complete in native child
    sessions; only the first and third show the subagent-routing banner and produce live gateway
    decisions correlated with those children. Claude's native task view reports no running
    tasks before /exit is submitted. No first-prompt routing wrapper starts.
    """
    session = live_session
    session.env["TMPDIR"] = str(tmp_path)
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    else:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    use_managed_config_fixture(session, "claude_smart_routing")
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
        _toggle_with_skill(
            tui,
            session,
            "claude",
            enabled=False,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "claude", "1+2", "3", routed=False)
        _toggle_with_skill(
            tui,
            session,
            "claude",
            enabled=True,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "claude", "2+2", "4", routed=True)
        tui.wait_for_background_tasks()
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
    canary = session.home / ".ucode" / "claude-smart-routing-canary.json"
    assert canary.is_file(), f"routing hooks were not armed: {canary}"


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.parametrize(
    "orchestration_enabled, SMART_ROUTER_CONFIG_VERSION",
    [
        (False, None),
        (True, None),
        (False, "subagent_only_v0"),
        (False, "subagent_only_v1"),
        (True, "subagent_orch_v0"),
    ],
    ids=[
        "routing-only",
        "orchestration",
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_router_skill_toggles_codex_subagent_routing(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Codex with the legacy flags or a subagent selector, spawn a child, invoke
    the installed Smart Router skill to turn routing off, spawn another child, turn routing
    back on through the skill, and spawn a third child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each invocation records the CLI
    confirmation in the native transcript and changes the saved routing controls, even with
    collapsed terminal output; all three uniquely tagged calculations complete in native child
    sessions; only the first and third show the subagent-routing banner and produce live gateway
    decisions correlated with those children. No first-prompt interposer starts.
    """
    session = live_session
    session.env["TMPDIR"] = str(tmp_path)
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    else:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    use_managed_config_fixture(session, "codex_smart_routing")
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
        _toggle_with_skill(
            tui,
            session,
            "codex",
            enabled=False,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "codex", "1+2", "3", routed=False)
        _toggle_with_skill(
            tui,
            session,
            "codex",
            enabled=True,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "codex", "2+2", "4", routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
