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
import subprocess
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
from utils.evidence import (
    FileTask,
    SubagentCalculation,
    agent_sessions,
    assert_subagent_routed,
    assistant_answers,
    codex_routing_display_for_task,
    fresh_id,
    read_jsonl,
)
from utils.harness import ANSI
from utils.managed import MANAGED_CONFIG_FIXTURES, use_smart_routing_fixture
from utils.request_recorder import TuiRequestRecorder, served_inference_request
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
    tui.submit(task.prompt)

    if routed:
        try:
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
        finally:
            session.record(
                f"routing-decisions-{task.marker}.json", _routing_decisions(session, agent)
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

    reload_log = session.home / ".ucode/codex-v2-interposer.log"
    before_reloads = reload_log.read_text().count("[RELOAD] resumed") if reload_log.exists() else 0
    tui.submit(invocation)
    approval_sent = False
    flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"
    python = (
        '& "$env:UCODE_SMART_ROUTER_PYTHON"' if os.name == "nt" else '"$UCODE_SMART_ROUTER_PYTHON"'
    )
    command = f"{python} -m ucode.cli codex {flag}"

    def toggled(screen):
        nonlocal approval_sent
        # Fresh remote Codex sessions can be read-only. Approve only this
        # requested toggle through the native dialog, keeping the sandbox intact.
        if not approval_sent and codex_command_approval_pending(screen, command):
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
            and reload_log.exists()
            and reload_log.read_text().count("[RELOAD] resumed") > before_reloads
            and "esc to interrupt" not in screen.lower()
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
    assert updated["task_name"] == payload["tool_input"]["task_name"], updated

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


SMART_ROUTER_VERSIONS = pytest.mark.parametrize(
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
    session, agent: str, workspace, tmp_path, orchestration_enabled, config_version
) -> None:
    """Choose the legacy flags, a subagent selector, or the managed default; install the fixture."""
    session.env["TMPDIR"] = str(tmp_path)
    if config_version is None:
        session.env["ENABLE_SMART_ROUTING_V2"] = "1"
        session.env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] = "1"
        if orchestration_enabled:
            session.env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] = "1"
    elif config_version != MANAGED_DEFAULT:
        session.env["SMART_ROUTER_CONFIG_VERSION"] = config_version
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
@SMART_ROUTER_VERSIONS
def test_smart_router_routes_claude_subagent_at_launch(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Claude with the legacy flags, a subagent selector, or only the managed
    config's smart routing default and, without touching the Smart Router skill, spawn one child in
    the real TUI.

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
        session, "claude", workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
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
    with TuiRequestRecorder(workspace) as recorder:
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
    with TuiRequestRecorder(workspace) as recorder:
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
def test_smart_router_routes_codex_subagent_at_launch(
    live_session, workspace, tmp_path, orchestration_enabled, SMART_ROUTER_CONFIG_VERSION
):
    """Scenario: launch Codex with the legacy flags or a subagent selector and, without touching the
    Smart Router skill, spawn one child in the real TUI.

    Expected: routing-only cases install Smart Router; enabling orchestration also installs
    Smart Router Orchestrator. The uniquely tagged calculation completes in a native child
    session, shows the subagent-routing banner (including [orchestrator on] when orchestration
    is enabled), and produces one live gateway decision correlated with that child.
    The control interposer starts without first-prompt model routing.
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
        session, "codex", [str(session.binary), "codex"], "smart-router-launch"
    ) as tui:
        tui.boot()
        _run_calculation(tui, session, "codex", "1+1", "2", orchestration_enabled, routed=True)
        tui.exit_normally()
        transcript = "".join(tui.output)
    assert SMART_ROUTING_BANNER not in transcript, transcript
    assert "[ROUTE]" not in (session.home / ".ucode/codex-v2-interposer.log").read_text()


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
    Smart Router Orchestrator. The invocation changes the saved routing controls,
    completes the conversation reload, and produces a parent answer.
    The uniquely tagged calculation completes in a native child session, shows no
    subagent-routing banner, and produces no live gateway decision. The control interposer starts without first-prompt model routing.
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
    assert "[ROUTE]" not in (session.home / ".ucode/codex-v2-interposer.log").read_text()


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
    Smart Router Orchestrator. Each invocation changes the saved routing controls
    (off, then restored), completes a conversation reload, and produces a parent answer.
    Each uniquely tagged calculation completes in a native child session. The
    first and third show the subagent-routing banner (including [orchestrator on] when
    orchestration is enabled) and produce one live gateway decision correlated with that child;
    the second shows no banner and produces no decision. The control interposer starts without first-prompt model routing.
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
    assert "[ROUTE]" not in (session.home / ".ucode/codex-v2-interposer.log").read_text()


def _codex_thread_ids(session) -> dict[str, str]:
    threads = {}
    for records in agent_sessions(session, "codex").values():
        for row in records:
            if row.get("type") != "session_meta":
                continue
            meta = row["payload"]
            source = meta.get("source")
            spawn = (
                source.get("subagent", {}).get("thread_spawn", {})
                if isinstance(source, dict)
                else {}
            )
            threads[spawn.get("agent_path", "/root")] = meta["id"]
    return threads


def _codex_thread_answered(session, thread_id: str, marker: str) -> bool:
    for records in agent_sessions(session, "codex").values():
        if any(
            row.get("type") == "session_meta" and row["payload"].get("id") == thread_id
            for row in records
        ):
            return any(marker in answer for answer in assistant_answers("codex", records))
    return False


def _codex_thread_turns(session, thread_ids) -> set[str]:
    return {
        row["payload"]["turn_id"]
        for records in agent_sessions(session, "codex").values()
        if any(
            row.get("type") == "session_meta" and row["payload"].get("id") in thread_ids
            for row in records
        )
        for row in records
        if row.get("type") == "turn_context"
    }


def _assert_codex_inference_recipes(
    session, recorder, checkpoint, thread_ids, recipe, *, continuation=None
):
    requests = recorder.requests_after(checkpoint)
    inferences = [
        request
        for request in requests
        if request.method == "POST"
        and request.path.endswith("/responses")
        and request.headers.get("thread-id") in thread_ids
    ]
    assert inferences, (thread_ids, checkpoint)
    if continuation:
        resumed = [
            request
            for request in inferences
            if continuation in json.dumps(request.payload.get("input", []))
        ]
        assert resumed, "No inference from the reload's continuation turn"
        inferences = [request for request in inferences if request.sequence >= resumed[0].sequence]
    assert {request.headers["thread-id"] for request in inferences} == set(thread_ids)
    evidence = []
    for request in inferences:
        metadata = request.payload.get("client_metadata", {}).get("x-codex-turn-metadata", {})
        metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
        evidence.append(
            {
                "sequence": request.sequence,
                "thread_id": request.headers["thread-id"],
                "model": request.payload.get("model"),
                "header_recipe": request.headers.get("databricks-smart-router-recipe"),
                "body_recipe": metadata.get("smart_router_recipe_name"),
            }
        )
    session.record(f"inference-recipes-after-{checkpoint}.json", evidence)
    for request, fields in zip(inferences, evidence, strict=True):
        if os.name == "nt":
            # requirements.toml owns the provider; dynamic attribution uses body metadata.
            assert fields["header_recipe"] in (None, recipe), fields
        else:
            assert fields["header_recipe"] == recipe, fields
        assert fields["body_recipe"] == recipe, fields
        served_inference_request(recorder, requests, request, "codex")
    return evidence


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
def test_smart_router_codex_agent_navigation_preserves_running_child(
    live_session, workspace, tmp_path
):
    """Scenario: open a running child and return to the main agent without a routing toggle.

    Expected: navigation never reloads conversations, interrupts the command, or injects a
    routing continuation. The original child completes its command exactly once and
    answers under its original ID; the parent accepts another turn. Reproduces on 0.167.0.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, True, "subagent_orch_v1")
    native_config = session.home / ".codex/config.toml"
    native_config.parent.mkdir(parents=True, exist_ok=True)
    native_config.write_text('approval_policy = "never"\nsandbox_mode = "workspace-write"\n')
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    marker = fresh_id()
    gate = session.cwd / "navigation-gate.py"
    python = session.binary.with_name("python.exe" if os.name == "nt" else "python")
    gate.write_text(
        "import os, time\nfrom pathlib import Path\n"
        "root = Path(__file__).parent\n"
        "with (root / 'command-starts').open('a') as starts: starts.write(str(os.getpid()) + '\\n')\n"
        "deadline = time.monotonic() + 180\n"
        "while not (root / 'release-child').exists():\n"
        "    if time.monotonic() >= deadline: raise TimeoutError('child gate')\n"
        "    time.sleep(0.05)\n"
        "(root / 'command-finished').write_text(str(os.getpid()))\n"
        f"print('child-done-{marker}')\n"
    )
    assignment = (
        f"Run {json.dumps(str(python))} {json.dumps(str(gate))} with no arguments. "
        f"Wait for it to finish, then reply child-done-{marker} as your FINAL answer and END "
        "your turn. Do not send_message or wait for another assignment afterward."
    )
    (session.cwd / "assignment.txt").write_text(assignment)
    with AgentTerminal(
        session, "codex", [str(session.binary), "codex"], "routing-navigation"
    ) as tui:

        def select_agent(label):
            heading = "Select an agent to watch"

            def selection():
                menu = tui.visible.partition(heading)[2]
                return next(
                    (line.strip() for line in menu.splitlines() if line.lstrip().startswith("›")),
                    "",
                )

            tui.submit("/subagents")
            tui.wait_for(lambda _: selection(), "subagent picker")
            for _ in range(2):
                current = selection()
                if label in current:
                    tui.send("\r", f"view {label}")
                    tui.wait_for(lambda screen: heading not in screen, f"opened {label}")
                    return
                tui.send("\x1b[B", f"move towards {label}")
                tui.wait_for(lambda _, before=current: selection() != before, "next subagent row")
            raise AssertionError(f"Subagent picker does not offer {label}:\n{tui.visible}")

        tui.boot()
        tui.submit(
            'Read assignment.txt. Spawn exactly one native child named nav_worker with fork_turns="none", '
            "using its contents as the exact assignment. "
            f"Root: after spawning, reply root-ready-{marker} and END your turn without waiting. "
            "This explicit instruction overrides orchestration ownership for this navigation test."
        )
        tui.wait_for(
            lambda _: (
                (session.cwd / "command-starts").exists()
                and "/root" in _codex_thread_ids(session)
                and _codex_thread_answered(
                    session, _codex_thread_ids(session)["/root"], f"root-ready-{marker}"
                )
            ),
            "running child command with idle parent",
            timeout=180,
        )
        initial = _codex_thread_ids(session)
        assert set(initial) == {"/root", "/root/nav_worker"}, initial
        starts = (session.cwd / "command-starts").read_text().splitlines()
        assert len(starts) == 1, starts
        reload_log = session.home / ".ucode/codex-v2-interposer.log"
        select_agent("nav_worker")
        tui.wait_for(
            lambda screen: "esc to interrupt" in screen.lower() or "unavailable" in screen.lower(),
            "running child view",
        )
        assert "[RELOAD" not in reload_log.read_text(), reload_log.read_text()
        assert not (session.cwd / "command-finished").exists()
        select_agent("Main")
        tui.wait_for(lambda screen: f"root-ready-{marker}" in screen, "original parent view")
        assert "[RELOAD" not in reload_log.read_text(), reload_log.read_text()

        (session.cwd / "release-child").write_text("release")
        tui.wait_for(
            lambda _: _codex_thread_answered(
                session, initial["/root/nav_worker"], f"child-done-{marker}"
            ),
            "original child's final answer",
            timeout=180,
        )
        assert (session.cwd / "command-starts").read_text().splitlines() == starts
        assert (session.cwd / "command-finished").read_text() == starts[0]
        tui.submit(f"Reply exactly parent-alive-{marker}. Do not use tools or delegate.")
        tui.wait_for(
            lambda _: _codex_thread_answered(session, initial["/root"], f"parent-alive-{marker}"),
            "parent answer after navigation",
            timeout=120,
        )
        assert _codex_thread_ids(session) == initial
        assert "[RELOAD" not in reload_log.read_text(), reload_log.read_text()
        for records in agent_sessions(session, "codex").values():
            assert not any(row.get("payload", {}).get("type") == "turn_aborted" for row in records)
            assert "UG reloaded this conversation" not in json.dumps(records)
        session.record("navigation-thread-identities.json", initial)
        tui.exit_normally()


def _codex_app_server_pids(tui):
    if os.name == "nt":
        output = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine "
                "| ConvertTo-Json -Compress",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        processes = [
            (row["ProcessId"], row["ParentProcessId"], row.get("CommandLine") or "")
            for row in json.loads(output)
        ]
    else:
        output = subprocess.run(
            ["ps", "-eww", "-o", "pid=,ppid=,args="],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        processes = [
            (int(pid), int(parent), args)
            for line in output.splitlines()
            for pid, parent, args in [line.strip().split(None, 2)]
        ]
    descendants = {tui.child.child.pid}
    while True:
        children = {pid for pid, parent, _ in processes if parent in descendants}
        if children <= descendants:
            break
        descendants.update(children)
    servers = sorted(
        pid
        for pid, _, command in processes
        if pid in descendants and "app-server" in command.split()
    )
    assert servers, "No native app-server under the launched ug process"
    return servers


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
@pytest.mark.parametrize("idle_children", [False, True], ids=["no-children", "idle-children"])
def test_smart_router_codex_idle_reload_updates_inference_without_recovery(
    live_session, workspace, tmp_path, idle_children
):
    """Scenario: toggle and repeat off/on through the skill with no children or two idle children.

    Expected: guidance precedes each toggle command and no automatic turn follows.
    Repeated settings leave controls untouched and do not reload. Explicit probes
    use current attribution. PIDs, connection, IDs, sandbox/approvals and exit survive.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, False, "subagent_only_v1")
    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        with AgentTerminal(
            session, "codex", [str(session.binary), "codex"], "reload-no-children"
        ) as tui:
            tui.boot()
            server_pids = _codex_app_server_pids(tui)
            session.record("app-server-launch-pids.json", server_pids)
            marker = fresh_id()
            tui.submit(f"Reply exactly ready-{marker}. Do not use tools or delegate.")
            tui.wait_for(
                lambda _: (
                    len(threads := _codex_thread_ids(session)) == 1
                    and _codex_thread_answered(session, threads["/root"], f"ready-{marker}")
                ),
                "initial parent answer",
                timeout=120,
            )
            original = _codex_thread_ids(session)
            root = original["/root"]
            if idle_children:
                tui.submit(
                    'Spawn exactly two children named idle_a and idle_b with fork_turns="none". '
                    f"Tell each to reply exactly child-idle-{marker} and END its turn. "
                    f"Wait for both final answers, then reply exactly root-idle-{marker} "
                    "and END your turn."
                )
                tui.wait_for(
                    lambda screen: (
                        set(threads := _codex_thread_ids(session))
                        == {"/root", "/root/idle_a", "/root/idle_b"}
                        and _codex_thread_answered(session, root, f"root-idle-{marker}")
                        and all(
                            _codex_thread_answered(session, threads[path], f"child-idle-{marker}")
                            for path in ("/root/idle_a", "/root/idle_b")
                        )
                        and "esc to interrupt" not in screen.lower()
                    ),
                    "parent and completed child idle before toggling",
                    timeout=120,
                )
                original = _codex_thread_ids(session)

            idle_threads = {path: tid for path, tid in original.items() if path != "/root"}
            original_idle_turns = _codex_thread_turns(session, idle_threads.values())
            idle_checkpoint = recorder.checkpoint()

            def root_records():
                return [
                    row
                    for records in agent_sessions(session, "codex").values()
                    if any(
                        row.get("type") == "session_meta" and row["payload"].get("id") == root
                        for row in records
                    )
                    for row in records
                ]

            def root_turns():
                return {
                    row["payload"]["turn_id"]
                    for row in root_records()
                    if row.get("type") == "turn_context"
                }

            requests = [r for r in recorder.requests_after(0) if r.path.endswith("/responses")]
            metadata = requests[0].payload["client_metadata"]["x-codex-turn-metadata"]
            metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
            launch_recipe = metadata["smart_router_recipe_name"]
            assert launch_recipe and launch_recipe != "DISABLED"
            for enabled, recipe in ((False, "DISABLED"), (True, launch_recipe)):
                before_turns = root_turns()
                before_records = len(root_records())
                _toggle_with_skill(
                    tui, session, "codex", enabled=enabled, orchestration_enabled=False
                )
                state = "on" if enabled else "off"
                assert _codex_app_server_pids(tui) == server_pids
                assert _codex_thread_ids(session) == original
                checkpoint = recorder.checkpoint()
                tui.submit(f"Reply exactly probe-{state}-{marker}. Do not use tools or delegate.")
                tui.wait_for(
                    lambda screen, state=state: (
                        _codex_thread_answered(session, root, f"probe-{state}-{marker}")
                        and "esc to interrupt" not in screen.lower()
                    ),
                    f"explicit parent probe after silent {state} reload",
                    timeout=120,
                )
                assert len(root_turns() - before_turns) == 2, "Toggle started an automatic turn"
                assert _codex_thread_ids(session) == original
                guidance = (
                    f"Turning Smart Router {state}. Codex may briefly show “Conversation interrupted” "
                    "while reloading routing settings. This is expected; you can continue "
                    "in the same conversation afterward."
                )
                new_items = [
                    row["payload"]
                    for row in root_records()[before_records:]
                    if row.get("type") == "response_item"
                ]
                guidance_position = next(
                    index
                    for index, item in enumerate(new_items)
                    if item.get("role") == "assistant"
                    and guidance
                    in " ".join(
                        " ".join(part.get("text", "") for part in item.get("content", [])).split()
                    )
                )
                flag = "--enable-smart-routing" if enabled else "--disable-smart-routing"
                command_position = next(
                    index
                    for index, item in enumerate(new_items)
                    if item.get("type") in {"function_call", "custom_tool_call"}
                    and flag in json.dumps(item)
                )
                assert guidance_position < command_position
                session.record(f"toggle-guidance-{state}.json", {"message": guidance})
                session.record(
                    f"recipe-{state}.json",
                    _assert_codex_inference_recipes(
                        session,
                        recorder,
                        checkpoint,
                        [root],
                        recipe,
                    ),
                )
                controls = list(Path(session.env["TMPDIR"]).glob("ug-session-env-*/env.json"))
                assert len(controls) == 1, controls
                before_controls = controls[0].stat()
                before_reloads = (session.home / ".ucode/codex-v2-interposer.log").read_text()
                before_turns = root_turns()
                before_answers = len(assistant_answers("codex", root_records()))
                tui.submit(f"$smart-router {state}")
                tui.wait_for(
                    lambda screen, before_answers=before_answers: (
                        len(assistant_answers("codex", root_records())) > before_answers
                        and "esc to interrupt" not in screen.lower()
                    ),
                    f"repeat {state} completes without reload",
                    timeout=120,
                )
                after_controls = controls[0].stat()
                assert (after_controls.st_ino, after_controls.st_mtime_ns) == (
                    before_controls.st_ino,
                    before_controls.st_mtime_ns,
                )
                assert (session.home / ".ucode/codex-v2-interposer.log").read_text().count(
                    "[RELOAD] resumed"
                ) == before_reloads.count("[RELOAD] resumed")
                assert len(root_turns() - before_turns) == 1
                assert _codex_app_server_pids(tui) == server_pids
                assert _codex_thread_ids(session) == original
                assert _codex_thread_turns(session, idle_threads.values()) == original_idle_turns
                assert not any(
                    request.path.endswith("/responses")
                    and request.headers.get("thread-id") in idle_threads.values()
                    for request in recorder.requests_after(idle_checkpoint)
                ), "Completed idle children sent inference after toggling"
            session.record(
                "idle-child-preservation.json",
                {"threads": idle_threads, "turns": sorted(original_idle_turns)},
            )
            log = (session.home / ".ucode/codex-v2-interposer.log").read_text()
            assert log.count("[CONN] TUI connected") == 1
            assert log.count("[RELOAD] resumed") == 2
            tui.exit_normally()


@pytest.mark.live
@pytest.mark.codex
@pytest.mark.managed_fixture
@pytest.mark.codex_smart_routing
@pytest.mark.parametrize("with_idle_child", [False, True], ids=["all-working", "mixed"])
def test_smart_router_codex_reload_updates_parent_and_existing_child_inference(
    live_session, workspace, tmp_path, with_idle_child
):
    """Scenario: toggle off with two children running, then on with three, optionally with an idle child.

    Expected: every resumed parent/child inference carries the new recipe in body metadata
    and the header (Windows permits an absent header). All original IDs and permissions
    survive; app-server PIDs and the TUI connection stay unchanged. The completed idle
    child starts no new turn or inference after either toggle. Resumed commands
    write exclusive completion receipts before children deliver
    their own final answers. Newly spawned children inherit the current recipe.
    Windows uses native approval for the exact toggle command;
    the workspace-write sandbox stays enabled. Requires real gateway responses and a normal TUI exit.
    """
    session = live_session
    _select_smart_router_version(session, "codex", workspace, tmp_path, True, "subagent_orch_v1")
    # User-owned native preferences; ug must preserve them through configure and reload.
    native_config = session.home / ".codex/config.toml"
    native_config.parent.mkdir(parents=True, exist_ok=True)
    approval_policy = "on-request" if os.name == "nt" else "never"
    native_config.write_text(
        f'approval_policy = "{approval_policy}"\nsandbox_mode = "workspace-write"\n'
    )
    marker = fresh_id()
    gate = session.cwd / "child-gate.py"
    python = session.binary.with_name("python.exe" if os.name == "nt" else "python")
    gate.write_text(
        "import sys, time\nfrom pathlib import Path\n"
        "root = Path(__file__).parent\nphase, name = sys.argv[1:]\n"
        "(root / f'{phase}-{name}-ready').write_text('ready')\n"
        "deadline = time.monotonic() + 180\n"
        "while not (root / f'release-{phase}').exists():\n"
        "    if time.monotonic() >= deadline: raise TimeoutError('child gate')\n"
        "    time.sleep(0.05)\n"
        "with (root / f'{phase}-{name}-completed').open('x') as receipt:\n"
        f"    receipt.write('{marker}')\n"
        f"print(f'child-{{phase}}-{{name}}-{marker}')\n"
    )

    def assignment(phase, name):
        return (
            f"Run the Python script {json.dumps(str(gate))} with arguments {phase} {name}, "
            f"using interpreter {json.dumps(str(python))}. Wait for its command to finish, "
            f"then output child-{phase}-{name}-{marker} as your FINAL assistant answer and "
            "END your turn. Do not send_message or wait for another assignment afterward."
        )

    def children_ready(phase, names):
        return all((session.cwd / f"{phase}-{name}-ready").exists() for name in names)

    def children_finished(phase, threads):
        return all(
            (receipt := session.cwd / f"{phase}-{path.rsplit('/', 1)[-1]}-completed").exists()
            and receipt.read_text() == marker
            and _codex_thread_answered(
                session, tid, f"child-{phase}-{path.rsplit('/', 1)[-1]}-{marker}"
            )
            for path, tid in threads.items()
        )

    def wait_for_reload(tui, expected_recipe, description):
        approval_sent = False
        flag = (
            "--disable-smart-routing" if expected_recipe == "DISABLED" else "--enable-smart-routing"
        )
        command = f'& "$env:UCODE_SMART_ROUTER_PYTHON" -m ucode.cli codex {flag}'

        def reloaded(screen):
            nonlocal approval_sent
            if (
                os.name == "nt"
                and not approval_sent
                and codex_command_approval_pending(screen, command)
            ):
                tui.send("\r", "approve the exact session-local Smart Router toggle once")
                approval_sent = True
                return False
            return f"recipe={expected_recipe}" in reload_log.read_text()[log_checkpoint:]

        tui.wait_for(reloaded, description, timeout=120)

    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-validate",
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        with AgentTerminal(
            session, "codex", [str(session.binary), "codex"], "routing-reload"
        ) as tui:
            tui.boot()
            app_server_pids = _codex_app_server_pids(tui)
            session.record("app-server-launch-pids.json", app_server_pids)
            checkpoint = recorder.checkpoint()
            idle_threads = {}

            def assert_idle_unchanged(since):
                assert _codex_thread_turns(session, idle_threads.values()) == original_idle_turns, (
                    "Completed idle child restarted"
                )
                assert not any(
                    request.path.endswith("/responses")
                    and request.headers.get("thread-id") in idle_threads.values()
                    for request in recorder.requests_after(since)
                ), "Completed idle child sent inference after the toggle"

            if with_idle_child:
                tui.submit(
                    'Spawn exactly one child named idle_worker with fork_turns="none". '
                    f"Tell it to reply exactly idle-child-{marker} and END its turn. "
                    f"Wait for its final answer, then reply exactly idle-parent-{marker} "
                    "and END your turn."
                )
                tui.wait_for(
                    lambda screen: (
                        set(threads := _codex_thread_ids(session)) == {"/root", "/root/idle_worker"}
                        and _codex_thread_answered(
                            session, threads["/root/idle_worker"], f"idle-child-{marker}"
                        )
                        and _codex_thread_answered(
                            session, threads["/root"], f"idle-parent-{marker}"
                        )
                        and "esc to interrupt" not in screen.lower()
                    ),
                    "completed child idle before launching working children",
                    timeout=120,
                )
                idle_threads = {
                    path: tid for path, tid in _codex_thread_ids(session).items() if path != "/root"
                }
            original_idle_turns = _codex_thread_turns(session, idle_threads.values())
            names = ("worker_a", "worker_b")
            assignments = {name: assignment("off", name) for name in names}
            (session.cwd / "off-assignments.json").write_text(json.dumps(assignments))
            tui.submit(
                'Read off-assignments.json. Spawn exactly two native children with fork_turns="none", '
                "using its keys as task names and values as exact assignments. "
                f"Root: after spawning both, reply root-started-{marker} and END your turn "
                "without waiting for them. This explicit instruction overrides orchestration "
                "ownership for this interruption test."
            )
            tui.wait_for(
                lambda _: (
                    children_ready("off", names)
                    and "/root" in _codex_thread_ids(session)
                    and _codex_thread_answered(
                        session, _codex_thread_ids(session)["/root"], f"root-started-{marker}"
                    )
                ),
                "two busy native children with idle parent",
                timeout=180,
            )
            initial = _codex_thread_ids(session)
            assert set(initial) == {"/root", "/root/worker_a", "/root/worker_b"} | set(
                idle_threads
            ), initial
            original_children = {
                path: tid
                for path, tid in initial.items()
                if path != "/root" and path not in idle_threads
            }
            assert all(
                not children_finished("off", {path: tid}) for path, tid in original_children.items()
            )
            launch_requests = [
                request
                for request in recorder.requests_after(checkpoint)
                if request.path.endswith("/responses")
                and request.headers.get("thread-id") == initial["/root"]
            ]
            assert launch_requests
            metadata = launch_requests[0].payload["client_metadata"]["x-codex-turn-metadata"]
            metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
            recipe = metadata.get("smart_router_recipe_name")
            assert recipe and recipe != "DISABLED"
            session.record(
                "recipe-launch.json",
                _assert_codex_inference_recipes(
                    session, recorder, checkpoint, initial.values(), recipe
                ),
            )

            off_checkpoint = recorder.checkpoint()
            reload_log = session.home / ".ucode/codex-v2-interposer.log"
            log_checkpoint = len(reload_log.read_text())
            tui.submit("$smart-router off")
            wait_for_reload(
                tui,
                "DISABLED",
                "conversations reloaded while both child commands were running",
            )
            off_pids = _codex_app_server_pids(tui)
            assert off_pids == app_server_pids
            session.record("app-server-off-pids.json", off_pids)
            (session.cwd / "release-off").write_text("release")
            tui.wait_for(
                lambda _: children_finished("off", original_children),
                "both original children recovered after off",
                timeout=180,
            )
            tui.wait_for(
                lambda screen: "esc to interrupt" not in screen.lower(),
                "parent idle after child recovery",
                timeout=180,
            )
            assert _codex_thread_ids(session) == initial
            _run_calculation(tui, session, "codex", "1+2", "3", False, routed=False)
            off_threads = _codex_thread_ids(session)
            assert len(off_threads) == 4 + len(idle_threads), off_threads
            assert_idle_unchanged(off_checkpoint)
            session.record(
                "recipe-off.json",
                _assert_codex_inference_recipes(
                    session,
                    recorder,
                    off_checkpoint,
                    [tid for path, tid in off_threads.items() if path not in idle_threads],
                    "DISABLED",
                    continuation="Smart Router is now off. UG reloaded",
                ),
            )

            existing_children = {
                path: tid
                for path, tid in off_threads.items()
                if path != "/root" and path not in idle_threads
            }
            assignments = {
                path: assignment("on", path.rsplit("/", 1)[-1]) for path in existing_children
            }
            (session.cwd / "on-assignments.json").write_text(json.dumps(assignments))
            tui.submit(
                "Read on-assignments.json. Use followup_task on EACH existing child named by a key, "
                "with its value as the exact assignment. Do not spawn replacements. "
                f"Root: after assigning all three, reply root-on-ready-{marker} and END your turn "
                "without waiting for them."
            )
            tui.wait_for(
                lambda _: (
                    children_ready("on", [path.rsplit("/", 1)[-1] for path in existing_children])
                    and _codex_thread_answered(session, initial["/root"], f"root-on-ready-{marker}")
                ),
                "all three existing children running before on",
                timeout=180,
            )
            assert all(
                not children_finished("on", {path: tid}) for path, tid in existing_children.items()
            )
            on_checkpoint = recorder.checkpoint()
            log_checkpoint = len(reload_log.read_text())
            tui.submit("$smart-router on")
            wait_for_reload(
                tui,
                recipe,
                "conversations reloaded while all three child commands were running",
            )
            on_pids = _codex_app_server_pids(tui)
            assert on_pids == app_server_pids
            session.record("app-server-on-pids.json", on_pids)
            (session.cwd / "release-on").write_text("release")
            tui.wait_for(
                lambda _: children_finished("on", existing_children),
                "all existing children recovered after on",
                timeout=180,
            )
            tui.wait_for(
                lambda screen: "esc to interrupt" not in screen.lower(),
                "parent idle after continuing all children",
                timeout=180,
            )
            session.record(
                "recipe-on-existing-children.json",
                _assert_codex_inference_recipes(
                    session,
                    recorder,
                    on_checkpoint,
                    [tid for path, tid in off_threads.items() if path not in idle_threads],
                    recipe,
                    continuation="Smart Router is now on. UG reloaded",
                ),
            )
            assert reload_log.read_text().count("[CONN] TUI connected") == 1
            _run_calculation(tui, session, "codex", "2+2", "4", True, routed=True)
            on_threads = _codex_thread_ids(session)
            assert len(on_threads) == 5 + len(idle_threads), on_threads
            assert all(on_threads[path] == tid for path, tid in off_threads.items())
            assert_idle_unchanged(on_checkpoint)
            session.record(
                "recipe-on.json",
                _assert_codex_inference_recipes(
                    session,
                    recorder,
                    on_checkpoint,
                    [tid for path, tid in on_threads.items() if path not in idle_threads],
                    recipe,
                    continuation="Smart Router is now on. UG reloaded",
                ),
            )
            assert _codex_app_server_pids(tui) == app_server_pids
            assert reload_log.read_text().count("[CONN] TUI connected") == 1
            session.record("app-server-pids.json", app_server_pids)
            session.record("thread-identities.json", on_threads)
            session.record(
                "idle-child-preservation.json",
                {"threads": idle_threads, "turns": sorted(original_idle_turns)},
            )
            session.record(
                "child-completion-receipts.json",
                {path.name: path.read_text() for path in session.cwd.glob("*-completed")},
            )
            for records in agent_sessions(session, "codex").values():
                contexts = [row["payload"] for row in records if row.get("type") == "turn_context"]
                assert contexts
                assert all(
                    context["sandbox_policy"]["type"] == "workspace-write"
                    and context["approval_policy"] == approval_policy
                    for context in contexts
                )
            tui.exit_normally()


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
