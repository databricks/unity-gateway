"""CUJs for subagent-only routing against the live workspace router.

Claude's native mod invokes ``ug claude-router-hook route-subagent-mod``; Codex's
PreToolUse hook invokes ``ug codex-router-hook route-subagent``. Both send JSON on
stdin. These journeys drive the real installed commands through that stdin contract,
so the routing decision, response shape, and audit trail are asserted without relying on
an agent choosing to spawn a subagent. The TUI journeys additionally invoke the installed
Smart Router skill and spawn real children before and after its session-local toggles.
"""

import json
import os
import re
from pathlib import Path

import pytest
from utils.evidence import (
    SubagentCalculation,
    agent_sessions,
    assert_subagent_routed,
    assistant_answers,
    codex_routing_display_for_task,
    is_child_session,
    read_jsonl,
    tool_outputs,
)
from utils.managed import MANAGED_CONFIG_FIXTURES, use_smart_routing_fixture
from utils.request_recorder import TuiRequestRecorder, served_inference_request
from utils.terminal import AgentTerminal

pytestmark = pytest.mark.harness_latest

SMART_ROUTING_BANNER = "Using Unity Gateway Smart Router."
SMART_ROUTING_SUBAGENT_NOTICE = "Using Unity Gateway Smart Router - Subagent"
# No selector or legacy flag: the managed config's smart_routing.enabled alone turns routing on.
MANAGED_DEFAULT = "managed-default"


# Use the same task_v3-compatible menu for direct hooks and managed TUI journeys.
# Direct hooks run in the US workspace; managed TUI inputs are assigned by workspace.
def _fixture_routing_models(agent: str) -> list[str]:
    config = json.loads((MANAGED_CONFIG_FIXTURES / f"{agent}_smart_routing.json").read_text())
    return config["enabled_agents"][0]["config"]["models"]["model_services"]


CLAUDE_MODELS = _fixture_routing_models("claude")
CODEX_MODELS = _fixture_routing_models("codex")
# Expected native Codex slugs, independently of the application's model mapper.
CODEX_MODEL_SLUGS = {"gpt-6-sol", "gpt-6-luna"}
SKILL_ROOTS = {"claude": ".claude/skills", "codex": ".codex/skills"}


def _routing_decisions(session, agent: str) -> list[dict]:
    return read_jsonl(session.home / ".ucode" / f"{agent}-smart-routing-decisions.jsonl")


def _claude_routing_row_for_task(lines: list[str], marker: str) -> bool:
    """Whether the mod's routing row follows the prompt that carries this task's marker.

    The parent chooses the Agent row's description, so the marker may appear only in the
    prompt. Each calculation is its own prompt, so the row is attributed by screen order:
    a selected-model line after the tagged prompt and before the next one.
    """
    prompts = [index for index, line in enumerate(lines) if line.lstrip().startswith("❯")]
    for position, start in enumerate(prompts):
        end = start + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        # The prompt wraps across indented lines; compare without rendered whitespace.
        if marker not in "".join("".join(lines[start:end]).split()):
            continue
        following = prompts[position + 1] if position + 1 < len(prompts) else len(lines)
        if any("↳ Selected Model:" in line for line in lines[end:following]):
            return True
    return False


def _routing_display_for_task(
    screen: str, marker: str, *, agent: str, orchestrator_on: bool
) -> bool:
    """Whether the rendered router panel belongs to this uniquely tagged task."""
    lines = screen.splitlines()
    if agent == "claude":
        return _claude_routing_row_for_task(lines, marker)
    return codex_routing_display_for_task(screen, marker, orchestrator_on=orchestrator_on)


def _run_calculation(
    tui,
    session,
    agent: str,
    expression: str,
    expected: str,
    orchestration_enabled: bool,
    *,
    routed: bool,
):
    task = SubagentCalculation(expression, expected)
    before = _routing_decisions(session, agent)
    tui.submit(task.prompt)

    if routed:
        tui.wait_for(
            lambda screen: _routing_display_for_task(
                screen,
                task.marker,
                agent=agent,
                orchestrator_on=orchestration_enabled,
            ),
            f"the Smart Router subagent display for {task.marker}",
            timeout=120,
        )
    tui.wait_for_task(task, timeout=180)
    # A parent may report the requested value while its background child is still
    # running. Require the child's own persisted answer before asserting completion.
    tui.wait_for(
        lambda _: task.completed(session, agent, child=True),
        f"the child assistant answer for {task.marker}",
        timeout=180,
    )
    task.assert_completed(session, agent)
    task.assert_completed(session, agent, child=True)

    after = _routing_decisions(session, agent)
    new_decisions = after[len(before) :]
    if not routed:
        assert not _routing_display_for_task(
            tui.visible, task.marker, agent=agent, orchestrator_on=False
        ), tui.visible
        assert not new_decisions, new_decisions
        return task

    assert len(new_decisions) == 1, new_decisions
    decision = new_decisions[0]
    assert task.marker in decision.get("task_name", ""), decision
    if agent == "claude":
        model = decision["requested_model"].removeprefix("system.ai.").removeprefix("databricks-")
        rationale = decision["rationale"]
        tui.wait_for(
            lambda screen: (
                f"SelectedModel:{model}" in "".join(screen.split())
                and (
                    not rationale
                    or "".join(f"Reason: {rationale}".split()) in "".join(screen.split())
                )
            ),
            f"the selected model and rationale for {task.marker}",
            timeout=30,
        )
    assert_subagent_routed(
        session,
        agent,
        task,
        decision_ids={decision["decision_id"]},
    )

    return task


def _codex_command_approval_pending(screen: str, expected_command: str) -> bool:
    prompt = "Would you like to run the following command?"
    if prompt not in screen or "Press enter to confirm or esc to cancel" not in screen:
        return False
    dialog = screen.rsplit(prompt, 1)[1]
    command = re.search(
        r"(?ms)^[ \t]*\$ (.*?)\n[ \t]*[›❯>][ \t]*1\. Yes, proceed \(y\)[ \t]*$",
        dialog,
    )
    assert (
        "Environment: local" in dialog
        and command is not None
        and command.group(1).strip() == expected_command
    ), "Codex requested an unexpected command approval:\n" + dialog
    return True


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
    confirmation = f"Smart Router is {state} for this session"
    if agent == "claude":
        # Claude's mod handles this locally; routing changes are checked by the next child.
        tui.submit(invocation)
        tui.wait_for(
            lambda screen: f"Smart Router is {state}" in screen,
            f"the Smart Router {state} confirmation",
            timeout=120,
        )
        return

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
    approval_sent = False
    flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"
    command = f'"$UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex {flag}'

    def toggled(screen):
        nonlocal approval_sent
        if os.name == "nt" and tui.approve_windows_command(screen):
            return False
        # Fresh remote Codex sessions can be read-only. Approve only this
        # requested toggle through the native dialog, keeping the sandbox intact.
        if not approval_sent and _codex_command_approval_pending(screen, command):
            tui.send("\r", f"approve the session-local Smart Router toggle {state} once")
            approval_sent = True
            return False
        try:
            controls_now = json.loads(controls[0].read_text())
        except PermissionError:
            # Windows denies reads while ug replaces the controls file; try again next frame.
            return False
        answers, confirmations = completion_counts()
        return (
            controls_now == expected
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
    """Scenario: the Claude mod's routing CLI receives an Agent call under legacy
    subagent routing or each supported selector.

    Expected: the shared Python client calls the real router and returns model,
    rationale, and an updated input naming the dispatched ``ucode-route-`` agent while
    preserving the task, so the mod labels the row with the agent that actually runs.
    One decision is audited for the session. This checks the mod's CLI protocol;
    no native mod or agent decides to spawn.
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
        "route-subagent-mod",
        "--host",
        workspace,
        *(arg for model in CLAUDE_MODELS for arg in ("--model", model)),
        input_text=json.dumps(payload),
        timeout=60,
    )
    assert result.stdout.strip(), f"Routing hook returned no decision: {result.stderr}"
    output = json.loads(result.stdout)
    assert output["model"] in CLAUDE_MODELS, output
    assert isinstance(output["rationale"], str), output
    updated = output["updatedInput"]
    assert "model" not in updated, updated
    assert updated["subagent_type"].startswith("ug-smart-router:ucode-route-"), updated
    assert updated["prompt"] == payload["tool_input"]["prompt"], updated
    assert updated["description"] == payload["tool_input"]["description"], updated
    assert "general-purpose" not in json.dumps(output), output

    decisions_path = session.home / ".ucode" / "claude-smart-routing-decisions.jsonl"
    assert decisions_path.is_file(), f"hook wrote no routing decision record: {decisions_path}"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    session.record("claude-smart-routing-decisions.jsonl", rows)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["session_id"] == payload["session_id"], row
    assert row["task_name"] == payload["tool_input"]["prompt"], row
    assert row["requested_model"] in CLAUDE_MODELS, row

    assert row["requested_model"] == output["model"], row
    assert row["rationale"] == output["rationale"], row


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
    assert result.stdout.strip(), f"Routing hook returned no decision: {result.stderr}"
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
@pytest.mark.soft_fail
@pytest.mark.parametrize(
    "orchestration_enabled, SMART_ROUTER_CONFIG_VERSION",
    [
        (False, None),
        (True, None),
        (True, MANAGED_DEFAULT),
        (False, "subagent_only_v0"),
        (False, "subagent_only_v1"),
        (True, "subagent_orch_v0"),
    ],
    ids=[
        "routing-only",
        "orchestration",
        MANAGED_DEFAULT,
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_router_skill_toggles_claude_subagent_routing(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Claude with the legacy flags, a subagent selector, or only the managed
    config's smart routing default, spawn a child, invoke the installed Smart Router skill to
    turn routing off, spawn another child, turn routing back on through the skill, and spawn a
    third child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each local invocation confirms the new state in the TUI.
    All three uniquely tagged calculations complete in native parent and child sessions;
    their recorded inference payloads carry smart_router_recipe_name with the launch
    recipe, DISABLED, then the launch recipe again, without a recipe header, and receive
    non-empty HTTP 200 responses (including verified native
    compatibility retries). Only the first and third show native model/rationale rows
    and produce live gateway decisions correlated with those children. Claude's native
    task view reports no running tasks before /exit. No first-prompt routing wrapper starts.
    """
    session = live_session
    session.env["TMPDIR"] = str(tmp_path)
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    elif SMART_ROUTER_CONFIG_VERSION != MANAGED_DEFAULT:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    # Managed smart routing would override the legacy env vars set above.
    use_smart_routing_fixture(
        session, "claude", workspace, smart_routing=SMART_ROUTER_CONFIG_VERSION is not None
    )
    with TuiRequestRecorder(workspace) as recorder:
        # Configure through the forwarding URL using the public CLI, keeping ug state real.
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        with AgentTerminal(
            session, "claude", [str(session.binary), "claude"], "smart-router-skill-toggle"
        ) as tui:
            tui.boot()
            launch_recipe = None
            for expression, result, routed in [
                ("1+1", "2", True),
                ("1+2", "3", False),
                ("2+2", "4", True),
            ]:
                if expression != "1+1":
                    _toggle_with_skill(
                        tui,
                        session,
                        "claude",
                        enabled=routed,
                        orchestration_enabled=orchestration_enabled,
                    )
                checkpoint = recorder.checkpoint()
                task = _run_calculation(
                    tui,
                    session,
                    "claude",
                    expression,
                    result,
                    orchestration_enabled if routed else False,
                    routed=routed,
                )
                requests = recorder.requests_after(checkpoint)
                inferences = [
                    request
                    for request in requests
                    if request.method == "POST"
                    and request.path == "/ai-gateway/anthropic/v1/messages"
                    and request.payload.get("tools")
                    and task.marker in json.dumps(request.payload.get("messages", []))
                ]
                assert inferences, f"No recorded inference for {task.marker}"
                if routed:
                    routes = [
                        request
                        for request in requests
                        if request.method == "POST"
                        and request.path == "/ai-gateway/routing/v1/routes:select"
                        and task.marker in request.payload.get("task", {}).get("prompt", "")
                    ]
                    assert len(routes) == 1, (task.marker, routes)
                    recipe = routes[0].payload.get("route_selector", {}).get("router_name")
                    assert isinstance(recipe, str) and recipe.strip() and recipe != "DISABLED", (
                        task.marker,
                        routes[0].sequence,
                        routes[0].payload.get("route_selector"),
                    )
                    if launch_recipe is None:
                        launch_recipe = recipe
                    else:
                        assert recipe == launch_recipe, (task.marker, recipe, launch_recipe)
                else:
                    recipe = "DISABLED"
                for request in inferences:
                    for tool in request.payload["tools"]:
                        if tool.get("name") not in {"Agent", "Task"}:
                            continue
                        description = tool["description"]
                        if orchestration_enabled and routed:
                            assert "before gathering source evidence" in description, description
                            assert "notification in a later turn" in description
                            assert (
                                "end the current turn and wait for that notification" in description
                            )
                            assert (
                                "To continue a previously spawned agent, use SendMessage"
                                in description
                            )
                            assert (
                                "starts a fresh agent with no memory of prior runs" in description
                            )
                            assert "When in doubt, don't spawn" not in description, description
                            assert "A fresh agent costs more than it looks" not in description, (
                                description
                            )
                        else:
                            assert (
                                "under the active Smart Router Orchestrator workflow"
                                not in description
                            )
                    assert request.payload.get("smart_router_recipe_name") == recipe, (
                        task.marker,
                        request.sequence,
                        request.payload.get("smart_router_recipe_name"),
                        recipe,
                    )
                    assert "databricks-smart-router-recipe" not in request.headers, (
                        task.marker,
                        request.sequence,
                    )
                    served_inference_request(recorder, requests, request, "claude")
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
        (True, MANAGED_DEFAULT),
        (False, "subagent_only_v0"),
        (False, "subagent_only_v1"),
        (True, "subagent_orch_v0"),
    ],
    ids=[
        "routing-only",
        "orchestration",
        MANAGED_DEFAULT,
        "subagent_only_v0",
        "subagent_only_v1",
        "subagent_orch_v0",
    ],
)
def test_smart_router_skill_toggles_codex_subagent_routing(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Codex with the legacy flags, a subagent selector, or only the managed
    config's smart routing default, spawn a child, invoke the installed Smart Router skill to
    turn routing off, spawn another child, turn routing back on through the skill, and spawn a
    third child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each invocation records the CLI
    confirmation in the native transcript and changes the saved routing controls, even with
    collapsed terminal output; all three uniquely tagged calculations complete in native child
    sessions; only the first and third show the subagent-routing banner and produce live gateway
    decisions correlated with those children. The session transport starts without making a
    first-prompt routing decision. POSIX permission prompts are approved only for the exact
    requested toggle command.
    With orchestration enabled, both routed banners must include [orchestrator on].
    """
    session = live_session
    session.env["TMPDIR"] = str(tmp_path)
    if SMART_ROUTER_CONFIG_VERSION is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    elif SMART_ROUTER_CONFIG_VERSION != MANAGED_DEFAULT:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
    # Managed smart routing would override the legacy env vars set above.
    use_smart_routing_fixture(
        session, "codex", workspace, smart_routing=SMART_ROUTER_CONFIG_VERSION is not None
    )
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
        _run_calculation(tui, session, "codex", "1+1", "2", orchestration_enabled, routed=True)
        _toggle_with_skill(
            tui,
            session,
            "codex",
            enabled=False,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "codex", "1+2", "3", False, routed=False)
        _toggle_with_skill(
            tui,
            session,
            "codex",
            enabled=True,
            orchestration_enabled=orchestration_enabled,
        )
        _run_calculation(tui, session, "codex", "2+2", "4", orchestration_enabled, routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    assert "[ROUTE]" not in session.routing_log("codex")
