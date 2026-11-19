"""CUJs for subagent-only routing against the live workspace router.

Claude's native mod invokes ``ug claude-router-hook route-subagent-mod``; Codex's
PreToolUse hook invokes ``ug codex-router-hook route-subagent``. Both send JSON on
stdin. These journeys drive the real installed commands through that stdin contract,
so the routing decision, response shape, and audit trail are asserted without relying on
an agent choosing to spawn a subagent. The TUI journeys additionally invoke the installed
Smart Router skill and spawn real children before and after its session-local toggles.
"""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from utils.agents.claude import (
    assert_follow_up_spawned_nothing,
    assert_follow_ups_resumed,
    assert_one_delegated_task,
    child_id,
    child_paths,
    claude_delivered_child_answers,
    parent_tool_calls,
)
from utils.agents.claude import (
    completed_task_models as claude_completed_task_models,
)
from utils.agents.codex import completed_command_outputs
from utils.evidence import (
    FileTask,
    SubagentCalculation,
    agent_sessions,
    assert_subagent_routed,
    assistant_answers,
    codex_routing_display_for_task,
    is_child_session,
    read_jsonl,
)
from utils.harness import ANSI
from utils.managed import (
    MANAGED_CONFIG_FIXTURES,
    use_managed_config_fixture,
    use_smart_routing_fixture,
)
from utils.request_recorder import (
    assert_served,
    record_claude_response_protocol,
    served_inference_request,
)
from utils.terminal import AgentTerminal, codex_command_approval_pending

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
    prompt = task.prompt
    if agent == "claude":
        # Native forks inherit their model and deliberately bypass Smart Router.
        prompt += ' Use the Agent tool with subagent_type "general-purpose"; do not fork or resume.'
    tui.submit(prompt)

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

    assert new_decisions, new_decisions
    if agent == "codex":
        # Correlate the audit hashes with plaintext assignments in native rollouts.
        assignments = set()
        for records in agent_sessions(session, agent).values():
            for row in records:
                item = row.get("payload", {})
                if row.get("type") != "response_item" or item.get("type") != "agent_message":
                    continue
                for part in item.get("content", []):
                    text = part.get("text", "")
                    if part.get("type") != "input_text" or not text.startswith(
                        "Message Type: NEW_TASK\n"
                    ):
                        continue
                    _, separator, assignment = text.partition("\nPayload:\n")
                    if separator and task.marker in assignment:
                        assignments.add(
                            hashlib.sha256(assignment.strip().encode("utf-8")).hexdigest()
                        )
        assert all(row["task_sha256"] in assignments for row in new_decisions), new_decisions
    else:
        assert all(task.marker in row.get("task_name", "") for row in new_decisions), new_decisions
    decision = new_decisions[0]
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
        decision_ids={decision["decision_id"] for decision in new_decisions},
    )

    return task


def _installed_skills(session, agent: str) -> list[str]:
    skill_root = session.home / SKILL_ROOTS[agent]
    if not skill_root.is_dir():
        return []
    ignored_skills = {".system"} if agent == "codex" else set()
    return sorted(
        path.name
        for path in skill_root.iterdir()
        if path.is_dir() and path.name not in ignored_skills
    )


def _toggle_with_skill(
    tui, session, agent: str, enabled: bool, orchestration_enabled: bool
) -> None:
    installed_skills = _installed_skills(session, agent)
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
            "ENABLE_SMART_ROUTER_AUTO_MODE": "0",
        }
    )
    assert json.loads(controls[0].read_text()) != expected

    flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"

    def confirmation_count():
        confirmations = 0
        for path, records in agent_sessions(session, agent).items():
            if is_child_session(agent, path, records):
                continue
            outputs = completed_command_outputs(
                records, command_contains=f"-m ucode.cli codex {flag}"
            )
            confirmations += sum(confirmation in output for output in outputs)
        return confirmations

    before_confirmations = confirmation_count()

    def root_answer_count():
        return sum(
            len(assistant_answers(agent, records))
            for path, records in agent_sessions(session, agent).items()
            if not is_child_session(agent, path, records)
        )

    before_answers = root_answer_count()
    tui.submit(invocation)
    approval_sent = False
    python = (
        '& "$env:UCODE_SMART_ROUTER_PYTHON"' if os.name == "nt" else '"$UCODE_SMART_ROUTER_PYTHON"'
    )
    command = f"{python} -m ucode.cli codex {flag}"
    commands = (command,)
    if os.name == "nt":
        commands += (
            "if (-not $env:UCODE_SMART_ROUTER_PYTHON -or -not $env:UCODE_SESSION_ENV_FILE) "
            '{ Write-Output "SMART_ROUTER_ENV_UNSET"; exit 2 }; ' + command,
            "if (-not $env:UCODE_SMART_ROUTER_PYTHON -or -not $env:UCODE_SESSION_ENV_FILE) "
            '{ Write-Output "SMART_ROUTER_ENV_UNSET" } else { ' + command + " }",
        )
    else:
        commands += (
            'test -n "$UCODE_SMART_ROUTER_PYTHON" && test -n "$UCODE_SESSION_ENV_FILE" && '
            + command,
        )

    def toggled(screen):
        nonlocal approval_sent
        # Fresh remote Codex sessions can be read-only. Approve only this
        # requested toggle through the native dialog, keeping the sandbox intact.
        if not approval_sent and codex_command_approval_pending(screen, commands):
            tui.send("\r", f"approve the session-local Smart Router toggle {state} once")
            approval_sent = True
            return False
        try:
            controls_now = json.loads(controls[0].read_text())
        except PermissionError:
            # Windows denies reads while ug replaces the controls file; try again next frame.
            return False
        return (
            controls_now == expected
            and confirmation_count() == before_confirmations + 1
            and root_answer_count() > before_answers
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

    Expected: the shared Python client calls the real router and returns only the
    spawn-ready model and rationale, leaving the mod's reviewed tool input intact.
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
    assert set(output) == {"model", "rationale"}, output

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
    assert updated["task_name"] == payload["tool_input"]["task_name"] + "__sr_on", updated

    decisions_path = session.home / ".ucode" / "codex-smart-routing-decisions.jsonl"
    assert decisions_path.is_file(), f"hook wrote no routing decision record: {decisions_path}"
    rows = [json.loads(line) for line in decisions_path.read_text().splitlines() if line.strip()]
    session.record("codex-smart-routing-decisions.jsonl", rows)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["session_id"] == payload["session_id"], row
    assignment = payload["tool_input"]["message"]
    assert row["task_sha256"] == hashlib.sha256(assignment.encode("utf-8")).hexdigest(), row
    assert row["task_length"] == len(assignment), row
    assert "task_name" not in row, row
    assert row["requested_model"] == updated["model"], row


SMART_ROUTER_CASES = [
    (False, None),
    (True, None),
    (True, MANAGED_DEFAULT),
    (False, "subagent_only_v0"),
    (False, "subagent_only_v1"),
    (True, "subagent_orch_v0"),
]
SMART_ROUTER_CASE_IDS = [
    "routing-only",
    "orchestration",
    MANAGED_DEFAULT,
    "subagent_only_v0",
    "subagent_only_v1",
    "subagent_orch_v0",
]
SMART_ROUTER_VERSIONS = pytest.mark.parametrize(
    "orchestration_enabled, SMART_ROUTER_CONFIG_VERSION",
    SMART_ROUTER_CASES,
    ids=SMART_ROUTER_CASE_IDS,
)

# GLM child sessions intermittently fail SubagentHandback, so GLM stays out of the shared Claude
# routing menu and gets one launch case whose menu offers only GLM.
CLAUDE_GLM_ROUTING_FIXTURE = "claude_smart_routing_glm"
CLAUDE_AT_LAUNCH_VERSIONS = pytest.mark.parametrize(
    "orchestration_enabled, SMART_ROUTER_CONFIG_VERSION, routing_fixture",
    [
        *((*case, None) for case in SMART_ROUTER_CASES),
        (False, "subagent_only_v0", CLAUDE_GLM_ROUTING_FIXTURE),
    ],
    ids=[*SMART_ROUTER_CASE_IDS, "subagent_only_v0-glm"],
)

# Codex is opted out under the managed default (subagent_orch_v0_claude_only), so these Codex
# journeys use only the legacy and explicit selector cases. The managed default for Codex is
# covered by test_smart_router_managed_default_leaves_codex_unrouted_at_launch.
CODEX_SMART_ROUTER_VERSIONS = pytest.mark.parametrize(
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


def _select_smart_router_version(
    session,
    agent: str,
    workspace,
    tmp_path,
    orchestration_enabled,
    config_version,
    routing_fixture: str | None = None,
) -> None:
    """Choose the legacy flags, a subagent selector, or the managed default; install the fixture.

    ``routing_fixture`` replaces the workspace's shared routing fixture for a selector case.
    """
    session.env["TMPDIR"] = str(tmp_path)
    if config_version is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    elif config_version != MANAGED_DEFAULT:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = config_version
    if routing_fixture is not None:
        assert config_version is not None, "a routing fixture enables managed smart routing"
        use_managed_config_fixture(session, routing_fixture)
        return
    # Managed smart routing would override the legacy env vars set above.
    use_smart_routing_fixture(session, agent, workspace, smart_routing=config_version is not None)


def _assert_claude_calculation_requests(
    recorder,
    checkpoint,
    task,
    *,
    routed: bool,
    launch_recipe: str | None,
    orchestration_enabled: bool,
) -> str:
    """Assert the recorded gateway traffic for one Claude calculation; return its recipe."""
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
        assert routes, (task.marker, routes)
        assert any(
            "Smart Router launch:" in json.dumps(request.payload.get("messages", []))
            for request in inferences
        ), "Parent inference must receive the routed child's launch identity"
        recipe = routes[0].payload.get("route_selector", {}).get("router_name")
        assert isinstance(recipe, str) and recipe.strip() and recipe != "DISABLED", (
            task.marker,
            routes[0].sequence,
            routes[0].payload.get("route_selector"),
        )
        if launch_recipe is not None:
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
                assert "end the current turn and wait for that notification" in description
                assert "To continue a previously spawned agent, use SendMessage" in description
                assert "starts a fresh agent with no memory of prior runs" in description
                assert "When in doubt, don't spawn" not in description, description
                assert "A fresh agent costs more than it looks" not in description, description
            else:
                assert "under the active Smart Router Orchestrator workflow" not in description
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
    return recipe


def _assert_claude_session_finished(session, tui) -> str:
    """Drain native tasks, exit the TUI, and return its transcript."""
    tui.wait_for_background_tasks()
    tui.exit_normally()
    return "".join(tui.output)


def _assert_claude_not_wrapped(session, transcript: str) -> None:
    assert SMART_ROUTING_BANNER not in transcript, transcript
    session.assert_not_routed()
    canary = session.home / ".ucode" / "claude-smart-routing-canary.json"
    assert canary.is_file(), f"routing hooks were not armed: {canary}"


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.managed_fixture
def test_smart_router_claude_resumes_same_subagent_with_send_message(
    live_session, workspace, tmp_path
):
    """Scenario: with orchestration on, spawn one Claude child, then give it a follow-up.

    Expected: on Claude 2.1.314+ the parent uses SendMessage to continue a routed
    child. Both hidden file values appear in that child's completed answers and reach
    the parent, without requiring the parent to restate them. The follow-up creates
    no child session or Agent/Task call, and SendMessage resolves to a pre-existing child.
    """
    session = live_session
    _select_smart_router_version(session, "claude", workspace, tmp_path, True, "subagent_orch_v0")
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    first = FileTask(session)
    with AgentTerminal(
        session, "claude", [str(session.binary), "claude"], "smart-router-resume-child"
    ) as tui:
        tui.boot()
        tui.submit(
            first.delegate_prompt
            + " Spawn exactly one agent in total. If Agent reports a background launch, "
            "end your turn and wait for its actual completion notification. Do not call "
            "Agent again to wait, or write a completion notification yourself."
        )
        tui.wait_for_task(
            first,
            completion=lambda: bool(
                claude_delivered_child_answers(agent_sessions(session, "claude"), first.value)
            ),
            description="the child's answer delivered to the parent",
        )
        tui.wait_for_background_tasks()
        pre_followup = agent_sessions(session, "claude")
        session.record("agent-sessions-before-followup.json", pre_followup)
        assert_one_delegated_task(pre_followup)

        second = FileTask(session)
        tui.submit(
            "Continue the same subagent using SendMessage and ask it to do this follow-up: "
            f"{second.prompt} Do not spawn another agent or read the file yourself. "
            "Wait for that agent's answer and reply with only the value it returned."
        )
        tui.wait_for_task(
            second,
            completion=lambda: bool(
                claude_delivered_child_answers(agent_sessions(session, "claude"), second.value)
            ),
            description="the follow-up child answer delivered to the parent",
        )
        _assert_claude_session_finished(session, tui)

    sessions = agent_sessions(session, "claude")
    session.record("agent-sessions.json", sessions)
    # The follow-up must resume an existing child: no new child session or spawn.
    assert_follow_up_spawned_nothing(pre_followup, sessions)
    followups = parent_tool_calls(sessions, {"SendMessage"})
    assert followups, "the follow-up sent no SendMessage"
    resumed_child = assert_follow_ups_resumed(
        list(followups.values()), [r for records in sessions.values() for r in records]
    )
    assert any(child_id(path) == resumed_child for path in child_paths(pre_followup)), (
        f"the follow-up resumed {resumed_child}, which is not one of the spawned children"
    )
    for task in (first, second):
        assert resumed_child in claude_delivered_child_answers(sessions, task.value), (
            f"Child {resumed_child} did not answer and deliver {task.value}"
        )


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.managed_fixture
@pytest.mark.soft_fail
@CLAUDE_AT_LAUNCH_VERSIONS
def test_smart_router_routes_claude_subagent_at_launch(
    live_session,
    workspace,
    tmp_path,
    orchestration_enabled,
    SMART_ROUTER_CONFIG_VERSION,
    routing_fixture,
):
    """Scenario: launch Claude with the legacy flags, a subagent selector, or only the managed
    config's smart routing default and, without touching the Smart Router skill, spawn one child in
    the real TUI. The ``subagent_only_v0-glm`` case offers only GLM, so its parent and routed
    child both run on GLM.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. The uniquely tagged calculation completes in native parent and
    child sessions; its recorded inference payloads carry smart_router_recipe_name with the
    launch recipe, without a recipe header, and receive non-empty HTTP 200 responses
    (including verified native compatibility retries). The child shows the native
    model/rationale row and produces one live gateway decision correlated with it. Claude's
    native task view reports no running tasks before /exit. No first-prompt routing wrapper
    starts.
    """
    session = live_session
    _select_smart_router_version(
        session,
        "claude",
        workspace,
        tmp_path,
        orchestration_enabled,
        SMART_ROUTER_CONFIG_VERSION,
        routing_fixture,
    )
    with record_claude_response_protocol(session, workspace) as recorder:
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
            session, "claude", [str(session.binary), "claude"], "smart-router-launch"
        ) as tui:
            tui.boot()
            checkpoint = recorder.checkpoint()
            task = _run_calculation(
                tui, session, "claude", "1+1", "2", orchestration_enabled, routed=True
            )
            _assert_claude_calculation_requests(
                recorder,
                checkpoint,
                task,
                routed=True,
                launch_recipe=None,
                orchestration_enabled=orchestration_enabled,
            )
            transcript = _assert_claude_session_finished(session, tui)
    _assert_claude_not_wrapped(session, transcript)


@pytest.mark.live
@pytest.mark.claude
def test_smart_router_claude_native_tree_routed_models_survive_reopen(live_session, workspace):
    """Scenario: configure Claude through ug, launch routed Claude, then open, close, and reopen
    the native subagent tree around a real delegated file task.

    Expected: the pane's router selection matches the persisted routing decision, its launched
    model matches the child's native API requests, and the parent and child answers remain
    present in the terminal and session evidence after closing and reopening the pane. The
    child api-request cross-check applies only where the CLI writes api-request rows into
    session transcripts (Claude 2.1.290, the CI pin, does not); there the recorded gateway
    traffic proves the child's inference, and the tree's reported launch identity and the
    persisted decision remain the evidence.
    """
    session = live_session
    session.env["SMART_ROUTER_CONFIG_VERSION"] = "subagent_only_v0"
    parent_model = session.model_for_explicit_case("claude")
    task = FileTask(session)
    with record_claude_response_protocol(session, workspace) as recorder:
        session.run(
            "configure",
            "--user",
            "--agents",
            "claude",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )

        with AgentTerminal(
            session,
            "claude",
            [
                str(session.binary),
                "claude",
                "--model",
                parent_model,
                "--enable-smart-routing",
            ],
            "smart-router-native-tree",
        ) as tui:
            tui.boot()
            checkpoint = recorder.checkpoint()
            tui.submit(task.delegate_prompt)
            tui.wait_for_task(
                task,
                completion=lambda: (
                    task.completed(session, "claude")
                    and bool(
                        claude_delivered_child_answers(
                            agent_sessions(session, "claude"), task.value
                        )
                    )
                ),
                description="the routed child answer delivered to its parent",
            )

            # An unsent draft typed into Main before the pane work must survive
            # every pane interaction below, including a native view round trip.
            draft = "Which safeguard matters most for trust? One sentence."
            tui.send(draft, "type the unsent Main draft")
            tui.wait_for(lambda screen: draft in screen, "the unsent draft in Main", timeout=12)

            sessions_before = agent_sessions(session, "claude")
            task.assert_completed(session, "claude")
            task.assert_completed(session, "claude", child=True)
            assert_one_delegated_task(sessions_before)
            assert_subagent_routed(session, "claude", task)

            child_models = {
                child_id(path): models
                for path, records in sessions_before.items()
                if is_child_session("claude", path, records)
                if (models := claude_completed_task_models(records, task.value))
            }
            assert child_models, "No completed child transcript reported its model"

            decisions = _routing_decisions(session, "claude")
            starts = read_jsonl(session.home / ".ucode" / "claude-smart-routing-audit.jsonl")
            routed_children = [
                (decision, start)
                for decision in decisions
                for start in starts
                if start.get("decision_id") == decision.get("decision_id")
                and start.get("agent_id") in child_models
            ]
            assert routed_children, "No completed child matched a persisted routing decision"

            tui.submit("/smart-router tree")
            tui.wait_for(
                lambda screen: "SUBAGENTS" in screen, "the native subagent tree", timeout=30
            )
            tui.send("\x1b[F", "scroll to the tree's completion summary")
            tui.wait_for(
                lambda screen: re.search(r"subagents \d+/\d+ done", screen) is not None,
                "the native subagent tree reporting the completed child",
                timeout=30,
            )
            tui.send("\x1b[H", "return to the first child card")
            tui.wait_for(lambda screen: "SUBAGENTS" in screen, "tree header", timeout=30)
            tui.click_label("details", "show the first child card's details")
            tui.wait_for(
                lambda screen: "Back to all subagents" in screen, "child details", timeout=30
            )
            tui.send("\x1b[F", "scroll to the child's model identity")

            tui.wait_for(
                lambda screen: "Launch model ·" in screen and "Router selection ·" in screen,
                "selected and launched model details",
                timeout=30,
            )
            selected_id = next(agent_id for agent_id in child_models if agent_id in tui.visible)
            decision, _ = next(
                pair for pair in routed_children if pair[1].get("agent_id") == selected_id
            )
            selected_model = decision["requested_model"]
            child_records = next(
                records
                for path, records in sessions_before.items()
                if child_id(path) == selected_id
            )
            # Claude 2.1.290 (the CI pin) writes no api-request rows into session transcripts;
            # newer CLIs do. Calibrate on the parent transcript: when the CLI records requests
            # there, the child's requests must carry exactly the selected model.
            parent_writes_api_requests = any(
                row.get("type") == "api-request"
                for path, records in sessions_before.items()
                if not is_child_session("claude", path, records)
                for row in records
            )
            requests = [row for row in child_records if row.get("type") == "api-request"]
            request_models = {row["params"]["model"] for row in requests}
            if parent_writes_api_requests:
                assert request_models == {selected_model.removesuffix("[1m]")}, request_models
                if selected_model.endswith("[1m]"):
                    assert all(
                        "context-1m-2025-08-07" in row["params"]["betas"] for row in requests
                    )
            response_models = child_models[selected_id]
            assert len(response_models) == 1, (selected_id, response_models)
            # Providers may report a deployment name instead of the requested catalog alias.
            launched_model = selected_model
            session.record(
                "tree-model-identity.json",
                {
                    "agent_id": selected_id,
                    "selected_model": selected_model,
                    "request_models": sorted(request_models),
                    "response_models": sorted(response_models),
                    "request_model_check": (
                        "child api-request rows"
                        if parent_writes_api_requests
                        else "recorded gateway traffic (assert_served)"
                    ),
                },
            )
            assert f"Router selection · {selected_model}" in tui.visible, tui.visible
            assert f"Launch model · {launched_model}" in tui.visible, tui.visible

            tui.send("\x1b", "close the native subagent tree")
            tui.wait_for(
                lambda screen: "SUBAGENTS" not in screen and task.value in screen,
                "the original parent answer after closing the tree",
                timeout=30,
            )
            tui.submit("/smart-router tree")
            tui.wait_for(
                lambda screen: "SUBAGENTS" in screen, "the reopened subagent tree", timeout=30
            )
            tui.send("\x1b[F", "scroll to the retained completion summary")
            tui.wait_for(
                lambda screen: re.search(r"subagents \d+/\d+ done", screen) is not None,
                "the reopened tree overview with the completed child",
                timeout=30,
            )
            tui.send("\x1b[H", "return to the retained child card")
            tui.wait_for(lambda screen: "SUBAGENTS" in screen, "retained tree header", timeout=30)
            tui.click_label("details", "reselect the same child after reopening")
            tui.wait_for(
                lambda screen: "Back to all subagents" in screen,
                "retained child details",
                timeout=30,
            )
            tui.send("\x1b[F", "scroll to the retained model identity")
            tui.wait_for(
                lambda screen: (
                    "Launch model ·" in screen
                    and "Router selection ·" in screen
                    and selected_id in screen
                ),
                "the reselected child with its retained model details",
                timeout=30,
            )
            assert f"Router selection · {selected_model}" in tui.visible, tui.visible
            assert f"Launch model · {launched_model}" in tui.visible, tui.visible

            # Back to the overview, then step into the child: a capable host
            # switches the main view to the child's own transcript (the pane
            # header shows Back to main); the CI pin's stub reopens the details.
            tui.click_label("Back to all subagents", "return to the overview")
            tui.wait_for(lambda screen: "SUBAGENTS" in screen, "the tree overview", timeout=30)
            tui.click_label("SUBAGENTS", "give the pane the keyboard")
            tui.send("1", "open the first child")
            tui.wait_for(
                lambda screen: "Back to all subagents" in screen or "Back to main" in screen,
                "the child opened from the tile",
                timeout=30,
            )
            if "Back to main" in tui.visible:
                tui.click_label("Back to main", "return the main view to Main")
                tui.wait_for(
                    lambda screen: task.value in screen and "Back to main" not in screen,
                    "the parent answer back in the main view",
                    timeout=30,
                )
            assert draft in tui.visible, "the unsent draft did not survive the view round trip"
            tui.send("\x1b", "close the reopened native subagent tree")
            tui.wait_for(
                lambda screen: "SUBAGENTS" not in screen and task.value in screen,
                "the original parent answer after reopening the tree",
                timeout=30,
            )
            # Independent launch evidence: the child's own inference in the
            # recorded gateway traffic, paired with a completed HTTP 200.
            routed_model = selected_model.removesuffix("[1m]")
            child_requests = [
                request
                for request in recorder.requests_after(checkpoint)
                if request.method == "POST"
                and request.path == "/ai-gateway/anthropic/v1/messages"
                and request.payload.get("model") == routed_model
            ]
            if parent_model.removesuffix("[1m]") == routed_model:
                # The parent shares the routed model; a marker filter cannot
                # separate them, so served-inference evidence stays model-level.
                child_requests = [
                    request
                    for request in child_requests
                    if task.marker in json.dumps(request.payload.get("messages", []))
                ]
            assert child_requests, (
                f"No recorded inference at the routed model {routed_model}; recorded models: "
                f"{sorted({request.payload.get('model') for request in recorder.requests_after(checkpoint) if request.path == '/ai-gateway/anthropic/v1/messages'})}"
            )
            # Every matching request must have asked for the routed model and
            # drawn a completed response; no selection of one survivor.
            for request in child_requests:
                assert_served(recorder, request, routed_model)
            tui.exit_normally()

    sessions_after = agent_sessions(session, "claude")
    task.assert_completed(session, "claude")
    task.assert_completed(session, "claude", child=True)
    children_after = {
        child_id(path): records
        for path, records in sessions_after.items()
        if is_child_session("claude", path, records)
    }
    assert selected_id in children_after, f"Selected child transcript disappeared: {selected_id}"
    assert child_models[selected_id] == claude_completed_task_models(
        children_after[selected_id], task.value
    )


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.managed_fixture
@pytest.mark.soft_fail
@SMART_ROUTER_VERSIONS
def test_smart_router_skill_turns_claude_subagent_routing_off(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Claude with the legacy flags, a subagent selector, or only the managed
    config's smart routing default, invoke the installed Smart Router skill to turn routing off,
    then spawn a child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. The local invocation confirms "Smart Router is off" in the TUI.
    The uniquely tagged calculation completes in native parent and child sessions; its recorded
    inference payloads carry smart_router_recipe_name DISABLED, without a recipe header, and
    receive non-empty HTTP 200 responses (including verified native compatibility retries).
    No native model/rationale row is shown, no route is selected, and no live gateway decision
    is recorded. Claude's native task view reports no running tasks before /exit. No
    first-prompt routing wrapper starts.
    """
    session = live_session
    _select_smart_router_version(
        session, "claude", workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
    )
    with record_claude_response_protocol(session, workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        with AgentTerminal(
            session, "claude", [str(session.binary), "claude"], "smart-router-skill-off"
        ) as tui:
            tui.boot()
            _toggle_with_skill(
                tui, session, "claude", enabled=False, orchestration_enabled=orchestration_enabled
            )
            checkpoint = recorder.checkpoint()
            task = _run_calculation(tui, session, "claude", "1+2", "3", False, routed=False)
            recipe = _assert_claude_calculation_requests(
                recorder,
                checkpoint,
                task,
                routed=False,
                launch_recipe=None,
                orchestration_enabled=orchestration_enabled,
            )
            assert recipe == "DISABLED", recipe
            transcript = _assert_claude_session_finished(session, tui)
    _assert_claude_not_wrapped(session, transcript)


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.managed_fixture
@pytest.mark.soft_fail
@SMART_ROUTER_VERSIONS
def test_smart_router_skill_turns_claude_subagent_routing_back_on(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Claude with the legacy flags, a subagent selector, or only the managed
    config's smart routing default, spawn a child, invoke the installed Smart Router skill to turn
    routing off, spawn a second child, turn routing back on, and spawn a third child in the same
    real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each local invocation confirms the new state in the TUI. All
    uniquely tagged calculations complete in native parent and child sessions. The second
    child's recorded inference payloads carry smart_router_recipe_name DISABLED, with no
    native model/rationale row and no live gateway decision. The first and third children show
    native model/rationale rows with live gateway decisions correlated with them; the third is
    routed again with the same recipe as the first, and both carry that recipe. All payloads
    omit a recipe header and receive non-empty HTTP 200 responses (including verified native
    compatibility retries). Claude's native task view reports no running tasks before /exit.
    No first-prompt routing wrapper starts.
    """
    session = live_session
    _select_smart_router_version(
        session, "claude", workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
    )
    with record_claude_response_protocol(session, workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        with AgentTerminal(
            session, "claude", [str(session.binary), "claude"], "smart-router-skill-back-on"
        ) as tui:
            tui.boot()
            checkpoint = recorder.checkpoint()
            launch_task = _run_calculation(
                tui, session, "claude", "1+1", "2", orchestration_enabled, routed=True
            )
            launch_recipe = _assert_claude_calculation_requests(
                recorder,
                checkpoint,
                launch_task,
                routed=True,
                launch_recipe=None,
                orchestration_enabled=orchestration_enabled,
            )
            _toggle_with_skill(
                tui, session, "claude", enabled=False, orchestration_enabled=orchestration_enabled
            )
            # Prove "off" takes effect after a routed child before turning routing back on.
            checkpoint = recorder.checkpoint()
            off_task = _run_calculation(tui, session, "claude", "1+2", "3", False, routed=False)
            recipe = _assert_claude_calculation_requests(
                recorder,
                checkpoint,
                off_task,
                routed=False,
                launch_recipe=None,
                orchestration_enabled=orchestration_enabled,
            )
            assert recipe == "DISABLED", recipe
            _toggle_with_skill(
                tui, session, "claude", enabled=True, orchestration_enabled=orchestration_enabled
            )
            checkpoint = recorder.checkpoint()
            task = _run_calculation(
                tui, session, "claude", "2+2", "4", orchestration_enabled, routed=True
            )
            _assert_claude_calculation_requests(
                recorder,
                checkpoint,
                task,
                routed=True,
                launch_recipe=launch_recipe,
                orchestration_enabled=orchestration_enabled,
            )
            transcript = _assert_claude_session_finished(session, tui)
    _assert_claude_not_wrapped(session, transcript)


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
@CODEX_SMART_ROUTER_VERSIONS
def test_smart_router_skill_turns_codex_subagent_routing_off(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Codex with the legacy flags or a subagent selector, invoke the installed
    Smart Router skill to turn routing off, then spawn a child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. The invocation records the CLI confirmation in the native
    transcript and changes the saved routing controls, even with collapsed terminal output.
    The uniquely tagged calculation completes in a native child session, shows no
    subagent-routing banner, and produces no live gateway decision. First-prompt
    model selection stays disabled; the metadata interposer remains active.
    """
    session = live_session
    _select_smart_router_version(
        session, "codex", workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
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
        session, "codex", [str(session.binary), "codex"], "smart-router-skill-off"
    ) as tui:
        tui.boot()
        _toggle_with_skill(
            tui, session, "codex", enabled=False, orchestration_enabled=orchestration_enabled
        )
        _run_calculation(tui, session, "codex", "1+2", "3", False, routed=False)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    assert "[ROUTE]" not in session.routing_log("codex")


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
@CODEX_SMART_ROUTER_VERSIONS
def test_smart_router_skill_turns_codex_subagent_routing_back_on(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Codex with the legacy flags or a subagent selector, spawn a child, invoke the
    installed Smart Router skill to turn routing off, spawn a second child, turn routing back on,
    and spawn a third child in the same real TUI session.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. Each invocation records the CLI confirmation in the native
    transcript and changes the saved routing controls (off, then restored), even with collapsed
    terminal output. Each uniquely tagged calculation completes in a native child session. The
    first and third show the subagent-routing banner (including [orchestrator on] when
    orchestration is enabled) and produce one live gateway decision correlated with that child;
    the second shows no banner and produces no decision. First-prompt model selection
    stays disabled; the metadata interposer remains active.
    """
    session = live_session
    _select_smart_router_version(
        session, "codex", workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
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
        session, "codex", [str(session.binary), "codex"], "smart-router-skill-back-on"
    ) as tui:
        tui.boot()
        _run_calculation(tui, session, "codex", "1+1", "2", orchestration_enabled, routed=True)
        _toggle_with_skill(
            tui, session, "codex", enabled=False, orchestration_enabled=orchestration_enabled
        )
        # Prove "off" takes effect after a routed child before turning routing back on.
        _run_calculation(tui, session, "codex", "1+2", "3", False, routed=False)
        _toggle_with_skill(
            tui, session, "codex", enabled=True, orchestration_enabled=orchestration_enabled
        )
        _run_calculation(tui, session, "codex", "2+2", "4", orchestration_enabled, routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    assert "[ROUTE]" not in session.routing_log("codex")


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
def test_smart_router_managed_default_leaves_codex_unrouted_at_launch(
    live_session, workspace, tmp_path
):
    """Scenario: launch Codex with only the managed config's smart_routing.enabled, which selects
    the Claude-only default preset, then spawn one tagged child in the real TUI without touching
    the Smart Router skill.

    Expected: ug warns that the workspace enables smart routing for Codex but it is currently
    only supported for Claude Code, and launches a plain, unrouted Codex session: neither
    `smart-router` nor Smart Router Orchestrator is installed in the fresh home. The tagged calculation completes in a native child session with no
    subagent-routing banner, produces no live gateway decision, and no first-prompt interposer
    starts.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, False, MANAGED_DEFAULT)
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "smart-router-managed-default"
    ) as tui:
        tui.boot()
        installed_skills = _installed_skills(session, "codex")
        assert "smart-router" not in installed_skills, installed_skills
        assert "smart-router-orchestrator" not in installed_skills, installed_skills
        _run_calculation(tui, session, "codex", "1+1", "2", False, routed=False)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    launch_text = " ".join(ANSI.sub("", transcript).split())
    assert (
        "Smart routing is enabled for Codex in this workspace, but it is currently only "
        "supported for Claude Code." in launch_text
    ), transcript
    session.assert_not_routed()
