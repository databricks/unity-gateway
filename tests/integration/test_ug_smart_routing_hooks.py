"""CUJs for the subagent-only smart-routing hook commands against the live workspace router.

The agent harness invokes ``ug claude-router-hook route-subagent`` /
``ug codex-router-hook route-subagent`` on its PreToolUse event with a JSON payload on
stdin. These journeys drive the real installed hook commands through that stdin contract,
so the routing decision, response shape, and audit trail are asserted without relying on
an agent choosing to spawn a subagent. The interactive spawn decision itself remains
uncovered; see the gaps matrix in tests/README.md.
"""

import json

import pytest
from utils.evidence import FileTask
from utils.terminal import AgentTerminal

# The same model lists as the managed_fixture smart-routing banner journeys, which are
# proven servable route options on the live e2e workspace.
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
    assert updated["subagent_type"].startswith("ucode-route-"), updated
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
def test_smart_routing_claude_subagent_only_launch_shows_no_first_prompt_banner(
    live_session, workspace
):
    """Scenario: configure Claude, then launch the real TUI with only subagent-only
    routing enabled and submit one file prompt.

    Expected: the routing hooks are armed (SessionStart canary), yet the prompt completes
    with no smart-routing banner and no first-prompt routing wrapper anywhere in the
    session, and the TUI exits normally. Only first-prompt silence is asserted here;
    subagent routing engagement is covered by the route-subagent hook journey above.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    session.run(
        "configure",
        "--agents",
        "claude",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    task = FileTask(session)
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "subagent-only-launch"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
    task.assert_completed(session, "claude")
    canary = session.home / ".ucode" / "claude-smart-routing-canary.json"
    assert canary.is_file(), f"routing hooks were not armed: {canary}"


@pytest.mark.live
@pytest.mark.codex
def test_smart_routing_codex_subagent_only_launch_shows_no_first_prompt_banner(
    live_session, workspace
):
    """Scenario: configure Codex, then launch the real TUI with only subagent-only
    routing enabled and submit one file prompt.

    Expected: the prompt completes with no smart-routing banner and no interposer
    first-prompt routing wrapper anywhere in the session, and the TUI exits normally.
    Only first-prompt silence is asserted here; subagent routing engagement is covered
    by the route-subagent hook journey above.
    """
    session = live_session
    session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
    session.run(
        "configure",
        "--agents",
        "codex",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    task = FileTask(session)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "subagent-only-launch"
    ) as tui:
        tui.boot()
        tui.submit(task.prompt)
        tui.wait_for_task(task)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
    task.assert_completed(session, "codex")
