"""Smart-routing CUJ: execute once, then report each contract assertion separately."""

import re
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from tests.integration.utils.evidence import (
    FileTask,
    agent_sessions,
    assert_subagent_routed,
    assistant_answers,
    is_child_session,
    read_jsonl,
)
from ucode.smart_routing.config import (
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
    SUBAGENT_ONLY_V0,
    SUBAGENT_ONLY_V1,
    SUBAGENT_ORCH_V0,
    SUBAGENT_ORCH_V0_CLAUDE_ONLY,
    SUBAGENT_ORCH_V1,
    resolve_environment,
)

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX, INFERENCE_PATHS, CodingAgent
from .helpers.evidence import (
    SessionEvidence,
    SessionObservation,
    canonical_model,
    claude_file_task,
    served_inference_request,
)
from .helpers.terminal import Terminal
from .helpers.tui_request_recorder import RecordedRequest, RecordedResponse
from .helpers.workspace import Workspace

CUJ_NAME = "CUJ 4 · Smart routing"
pytestmark = pytest.mark.harness_latest

ROUTING_PATH = "/ai-gateway/routing/v1/routes:select"
AGENTS = (CLAUDE, CODEX)
ORCHESTRATOR_CONTEXT = "Smart Router Orchestrator is on for this session."
SMART_ROUTER_CONFIG_VERSIONS = (
    FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0,
    SUBAGENT_ONLY_V0,
    SUBAGENT_ONLY_V1,
    SUBAGENT_ORCH_V0,
    SUBAGENT_ORCH_V1,
    SUBAGENT_ORCH_V0_CLAUDE_ONLY,
)


def _smart_router_band_shown(tui, *, enabled, first_prompt):
    state = "ON" if enabled else "OFF"
    pattern = rf"\s*unity gateway smart router\s+{state}\b"
    if enabled:
        scope = "first prompt + subagents" if first_prompt else "subagents"
        pattern += rf"\s+—\s+routing\s+{re.escape(scope)}\b"
    return any(re.match(pattern, line) for line in tui.screen.display)


def _smart_router_hint_shown(tui, *, enabled):
    """The prompt hint row under the input ends with the current Smart Router state."""
    state = "ON" if enabled else "OFF"
    pattern = rf"(^|·)\s*smart router {state}$"
    return any(re.search(pattern, line.strip()) for line in tui.screen.display)


def _smart_router_band_hidden(tui):
    """The band above the prompt shows only for the turn after launch or a toggle."""
    pattern = r"\s*unity gateway smart router\s+(ON|OFF)\b"
    return not any(re.match(pattern, line) for line in tui.screen.display)


@dataclass(frozen=True)
class PresetFlags:
    subagent_routed: bool
    first_prompt_routed: bool
    orchestrator_enabled: bool


def _preset_flags(version, agent):
    """Resolve the preset's routing and orchestration expectations for one agent."""
    env = resolve_environment({"SMART_ROUTER_CONFIG_VERSION": version}, agent=agent)
    v2_enabled = env["ENABLE_SMART_ROUTING_V2"] == "1"
    subagent_only = env["ENABLE_SMART_ROUTING_SUBAGENT_ONLY"] == "1"
    subagent_routed = v2_enabled or subagent_only
    return PresetFlags(
        subagent_routed=subagent_routed,
        first_prompt_routed=v2_enabled and not subagent_only,
        orchestrator_enabled=env["ENABLE_SMART_ROUTER_ORCHESTRATOR"] == "1" and subagent_routed,
    )


@contextmanager
def smart_router_preset(cuj, version):
    """Select a preset for one fresh ug configuration, then restore env and machine-wide settings."""
    session, workspace, recorder = cuj
    previous = session.env.get("SMART_ROUTER_CONFIG_VERSION")
    session.env["SMART_ROUTER_CONFIG_VERSION"] = version
    try:
        configs = _assert_published_config_matches_expectations(workspace.config())
        recorder.configure_session(session, ["configure", "--disable-databricks-ai-tools"])
        yield session, workspace, recorder, configs
    finally:
        if previous is None:
            session.env.pop("SMART_ROUTER_CONFIG_VERSION", None)
        else:
            session.env["SMART_ROUTER_CONFIG_VERSION"] = previous
        session.revert_machine_wide(
            f"{version}-revert",
            "Smart-router preset cleanup left machine-wide agent settings",
        )


@dataclass(frozen=True)
class SessionCase:
    agent: str
    launch_args: tuple[str, ...]
    task: FileTask
    observation: SessionObservation
    requests: tuple[RecordedRequest, ...]
    inference_request: RecordedRequest
    inference_response: RecordedResponse


@dataclass(frozen=True)
class SmartRoutingSessionResults:
    supported: dict[str, set[str]]
    defaults: dict[str, str]
    overrides: dict[str, str]
    no_model_override: dict[str, SessionCase]
    with_model_override: dict[str, SessionCase]


def _file_task(session, agent):
    task = claude_file_task(session) if agent == CLAUDE else FileTask(session)
    task.prompt += " Do not delegate."
    return task


def _run_session(session, recorder, agent, task, launch_args):
    evidence = SessionEvidence(session.home, agent)
    checkpoint = recorder.checkpoint()
    recorder.prepare_launch()
    with Terminal(session, "-".join(launch_args), list(launch_args)) as tui:
        tui.boot(timeout=150)
        tui.submit(task.prompt)
        tui.task(evidence, task)
        tui.exit_normally()
    return evidence.observe(task), recorder.requests_after(checkpoint)


def _task_inference_request(requests, agent, prompt, *, after=0):
    """Match plaintext task prompts or Codex's encrypted child task name."""
    prompts = {prompt}
    if agent == CLAUDE and after:
        prompts.add(prompt + "\n")
    for request in requests:
        if (
            request.sequence <= after
            or request.method != "POST"
            or request.path != INFERENCE_PATHS[agent]
        ):
            continue
        payload = request.payload
        entries = payload.get("messages" if agent == CLAUDE else "input", [])
        tools = payload.get("tools")
        if agent == CODEX and not tools and isinstance(entries, list):
            # Responses Lite carries tool definitions in developer input items.
            tools = next(
                (
                    entry.get("tools")
                    for entry in entries
                    if isinstance(entry, dict)
                    and entry.get("type") == "additional_tools"
                    and entry.get("role") == "developer"
                ),
                None,
            )
        if not isinstance(tools, list) or not tools:
            continue
        if isinstance(entries, str):
            if entries in prompts:
                return request
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            # Encrypted Codex child instructions leave only task_name readable.
            if (
                agent == CODEX
                and after
                and isinstance(entry, dict)
                and entry.get("type") == "agent_message"
                and isinstance(entry.get("recipient"), str)
                and entry["recipient"].rsplit("/", 1)[-1] == prompt
                and any(
                    isinstance(part, dict)
                    and part.get("type") == "encrypted_content"
                    and isinstance(part.get("encrypted_content"), str)
                    and part["encrypted_content"]
                    for part in entry.get("content", [])
                )
            ):
                return request
            if not isinstance(entry, dict) or entry.get("role") != "user":
                continue
            content = entry.get("content", [])
            if isinstance(content, str):
                if content in prompts:
                    return request
            elif isinstance(content, list) and any(
                isinstance(part, dict)
                and part.get("type") in {"text", "input_text"}
                and isinstance(part.get("text"), str)
                and part.get("text") in prompts
                for part in content
            ):
                return request
    raise AssertionError("No tool-capable inference request contained the exact task prompt")


def _assert_published_config_matches_expectations(published):
    assert published["spec_version"] == 1
    assert published["default_agent"] == CodingAgent.CLAUDE_CODE
    entries = published["enabled_agents"]
    assert len(entries) == 2
    configs = Workspace.agent_configs(published)
    assert set(configs) == {CodingAgent.CLAUDE_CODE, CodingAgent.CODEX}
    agent_configs = {
        CLAUDE: configs[CodingAgent.CLAUDE_CODE],
        CODEX: configs[CodingAgent.CODEX],
    }

    for config in agent_configs.values():
        assert config["smart_routing"]["enabled"] is True
        assert config.get("tracing", {}).get("enabled", False) is False
        assert not config.get("smart_defaults") and not config.get("spend_tiers")
        assert set(config["models"]) == {"model_services"}, (
            "No MPS or schema source is allowed in this CUJ"
        )
        offered = config["models"]["model_services"]
        assert len(set(offered)) >= 2
        assert config["default_models"]["default_model"] in offered

    return agent_configs


def run_smart_routing_journeys(cuj) -> SmartRoutingSessionResults:
    """Run each live journey and collect the evidence used by the tests."""
    session, workspace, recorder = cuj
    published = workspace.config()
    agent_configs = _assert_published_config_matches_expectations(published)

    supported, defaults, overrides = {}, {}, {}
    for agent, config in agent_configs.items():
        offered = config["models"]["model_services"]
        supported[agent] = workspace.model_ids(agent)
        defaults[agent] = config["default_models"]["default_model"]
        overrides[agent] = next(model for model in offered if model != defaults[agent])

    recorder.configure_session(
        session,
        ["configure", "--disable-databricks-ai-tools"],
    )

    no_model_override, with_model_override = {}, {}
    for agent in AGENTS:
        task = _file_task(session, agent)
        observation, requests = _run_session(session, recorder, agent, task, (agent,))
        inference_request = _task_inference_request(requests, agent, task.prompt)
        inference_request = served_inference_request(recorder, requests, inference_request, agent)
        no_model_override[agent] = SessionCase(
            agent=agent,
            launch_args=(agent,),
            task=task,
            observation=observation,
            requests=requests,
            inference_request=inference_request,
            inference_response=recorder.response_for(inference_request),
        )

        task = _file_task(session, agent)
        launch_args = (agent, "--model", overrides[agent])
        observation, requests = _run_session(session, recorder, agent, task, launch_args)
        inference_request = _task_inference_request(requests, agent, task.prompt)
        inference_request = served_inference_request(recorder, requests, inference_request, agent)
        with_model_override[agent] = SessionCase(
            agent=agent,
            launch_args=launch_args,
            task=task,
            observation=observation,
            requests=requests,
            inference_request=inference_request,
            inference_response=recorder.response_for(inference_request),
        )

    return SmartRoutingSessionResults(
        supported=supported,
        defaults=defaults,
        overrides=overrides,
        no_model_override=no_model_override,
        with_model_override=with_model_override,
    )


@pytest.fixture(scope="class")
def completed_smart_routing_runs(cuj):
    """Run all live journeys once for this CUJ suite and return their captured evidence."""
    return run_smart_routing_journeys(cuj)


class TestCujSmartRouting(BaseCujTest):
    WORKSPACE_URL = "https://dbc-1a9622fc-2e91.cloud.databricks.com/"

    @pytest.mark.parametrize("agent", AGENTS)
    @pytest.mark.parametrize("SMART_ROUTER_CONFIG_VERSION", SMART_ROUTER_CONFIG_VERSIONS)
    def test_smart_router_preset_routes_first_prompt_then_explicit_native_subagent(
        self, cuj, agent, SMART_ROUTER_CONFIG_VERSION
    ):
        """Scenario: launch one agent with a preset, run a no-delegation file task, then ask for a subagent.

        Expected: the agent's resolved flags determine whether the first prompt is routed
        (one HTTP 200 decision matching the prompt, and the routed model serves inference),
        whether orchestrator context and the smart-router recipe header/field are present,
        and that no child session exists after the first prompt. A second prompt then makes
        a native child complete the delegated file task even when routing is disabled.
        When subagent routing is on, a decision correlated with the child task is recorded,
        the child's inference uses the selected model, and the recipe correlates with the
        launch turn; when off, no decision, recipe, or orchestrator context appears. Claude
        then reports no running background tasks and exits cleanly.
        """
        with smart_router_preset(cuj, SMART_ROUTER_CONFIG_VERSION) as (
            session,
            workspace,
            recorder,
            configs,
        ):
            flags = _preset_flags(SMART_ROUTER_CONFIG_VERSION, agent)
            subagent_routed = flags.subagent_routed
            first_prompt_routed = flags.first_prompt_routed
            orchestrator_enabled = flags.orchestrator_enabled
            supported = workspace.model_ids(agent)
            evidence = SessionEvidence(session.home, agent)
            existing_sessions = set(agent_sessions(session, agent))
            decisions_path = session.home / ".ucode" / f"{agent}-smart-routing-decisions.jsonl"
            original_decisions = read_jsonl(decisions_path)
            task = FileTask(session)
            task.prompt += " Do not delegate."
            checkpoint = recorder.checkpoint()
            recorder.prepare_launch()
            with Terminal(session, f"{SMART_ROUTER_CONFIG_VERSION}-{agent}", [agent]) as tui:
                tui.boot(timeout=150)
                tui.submit(task.prompt)
                tui.task(evidence, task)
                requests = recorder.requests_after(checkpoint)
                routes = [request for request in requests if request.path == ROUTING_PATH]
                assert len(routes) == int(first_prompt_routed), (agent, routes)
                expected_model = configs[agent]["default_models"]["default_model"]
                if first_prompt_routed:
                    assert routes[0].payload["task"]["prompt"] == task.prompt
                    response = recorder.response_for(routes[0])
                    assert response.status_code == 200
                    selections = response.payload["route_selection"]
                    assert len(selections) == 1
                    expected_model = selections[0]["route_option"]["model"]
                    options = routes[0].payload["route_options"]
                    offered = {canonical_model(option["model"]) for option in options}
                    assert options and all(option["harness"] == agent for option in options)
                    assert offered <= {canonical_model(model) for model in supported}
                    selected = canonical_model(expected_model)
                    assert selected in offered and selected.startswith("system.ai."), selected
                inference = _task_inference_request(requests, agent, task.prompt)
                inference = served_inference_request(recorder, requests, inference, agent)
                assert canonical_model(inference.payload["model"]) == canonical_model(
                    expected_model
                )
                evidence.assert_applied(task, supported, expected=expected_model)
                if agent == CLAUDE:
                    launch_recipe = inference.payload.get("smart_router_recipe_name")
                    assert (
                        isinstance(launch_recipe, str)
                        and launch_recipe.strip()
                        and launch_recipe != "DISABLED"
                    )
                    if first_prompt_routed:
                        assert routes[0].payload["route_selector"]["router_name"] == launch_recipe
                    assert "databricks-smart-router-recipe" not in inference.headers
                else:
                    assert (
                        "databricks-smart-router-recipe" in inference.headers
                    ) == subagent_routed, agent
                assert (ORCHESTRATOR_CONTEXT.encode() in inference.body) == orchestrator_enabled, (
                    agent
                )
                assert not any(
                    is_child_session(agent, path, records)
                    for path, records in agent_sessions(session, agent).items()
                    if path not in existing_sessions
                ), agent

                child_task = FileTask(session)
                child_task.prompt = child_task.delegate_prompt
                decision_count = len(read_jsonl(decisions_path))
                checkpoint = recorder.checkpoint()
                existing_sessions = set(agent_sessions(session, agent))
                tui.submit(child_task.prompt)
                # The parent can resume a finished child before replying; wait for its
                # reply so /tasks sees that run.
                tui.wait_until(
                    lambda: (
                        child_task.completed(session, agent, child=True)
                        and child_task.completed(session, agent)
                    ),
                    "completed native subagent file task and parent reply",
                )
                children = {
                    path: records
                    for path, records in agent_sessions(session, agent).items()
                    if path not in existing_sessions and is_child_session(agent, path, records)
                }
                completed_children = {
                    path: records
                    for path, records in children.items()
                    if any(
                        child_task.value in answer for answer in assistant_answers(agent, records)
                    )
                }
                assert completed_children, (agent, children.keys())
                decisions = read_jsonl(decisions_path)[decision_count:]
                decision_tasks = {
                    decision.get("task_name") for decision in decisions if decision.get("task_name")
                }
                requests = recorder.requests_after(checkpoint)
                routes = [
                    request
                    for request in requests
                    if request.path == ROUTING_PATH
                    and request.payload.get("task", {}).get("prompt") in decision_tasks
                ]
                assert bool(routes) == subagent_routed, (agent, routes)
                if subagent_routed:
                    assert_subagent_routed(
                        session,
                        agent,
                        child_task,
                        decision_ids={decision["decision_id"] for decision in decisions},
                    )
                    route_prompt = routes[0].payload["task"]["prompt"]
                    assert isinstance(route_prompt, str) and route_prompt.strip()
                    if agent == CLAUDE:
                        assert child_task.filename in route_prompt
                    response = recorder.response_for(routes[0])
                    assert response.status_code == 200
                    selections = response.payload["route_selection"]
                    assert len(selections) == 1
                    inference = _task_inference_request(
                        requests,
                        agent,
                        route_prompt,
                        after=routes[0].sequence,
                    )
                    inference = served_inference_request(recorder, requests, inference, agent)
                    assert canonical_model(inference.payload["model"]) == canonical_model(
                        selections[0]["route_option"]["model"]
                    )
                    if agent == CLAUDE:
                        assert routes[0].payload["route_selector"]["router_name"] == launch_recipe
                        assert inference.payload.get("smart_router_recipe_name") == launch_recipe
                        assert "databricks-smart-router-recipe" not in inference.headers
                    else:
                        assert "databricks-smart-router-recipe" in inference.headers
                else:
                    assert read_jsonl(decisions_path) == original_decisions
                    assert not any(
                        "databricks-smart-router-recipe" in request.headers
                        or "smart_router_recipe_name" in request.payload
                        or ORCHESTRATOR_CONTEXT.encode() in request.body
                        for request in requests
                    ), agent
                if agent == CLAUDE:
                    tui.wait_for_background_tasks()
                tui.exit_normally()

    @pytest.mark.parametrize("SMART_ROUTER_CONFIG_VERSION", SMART_ROUTER_CONFIG_VERSIONS)
    def test_smart_router_preset_claude_status_band_and_hint_row_follow_launch_and_toggles(
        self, cuj, SMART_ROUTER_CONFIG_VERSION
    ):
        """Scenario: launch Claude with a preset, run one file task, then toggle /smart-router off and on.

        Expected: at launch the status band shows ON with the preset's routing scope and the
        prompt hint row ends with smart router ON. After the launch turn completes the band is
        hidden and the hint row still shows ON. Each toggle (off, then on) shows the matching
        band, flips the hint row immediately, and after a following completed file task the
        band is hidden again while the hint row keeps the toggled state. No delegation or
        background-task wait is involved.
        """
        with smart_router_preset(cuj, SMART_ROUTER_CONFIG_VERSION) as (session, _, recorder, _):
            first_prompt_routed = _preset_flags(
                SMART_ROUTER_CONFIG_VERSION, CLAUDE
            ).first_prompt_routed
            evidence = SessionEvidence(session.home, CLAUDE)
            task = FileTask(session)
            task.prompt += " Do not delegate."
            recorder.prepare_launch()
            with Terminal(session, f"{SMART_ROUTER_CONFIG_VERSION}-claude-band", [CLAUDE]) as tui:
                tui.boot(timeout=150)
                tui.wait_for(
                    lambda _: _smart_router_band_shown(
                        tui, enabled=True, first_prompt=first_prompt_routed
                    ),
                    "the ON Smart Router band with the preset's routing scope",
                )
                tui.wait_for(
                    lambda _: _smart_router_hint_shown(tui, enabled=True),
                    "smart router ON in the prompt hint row",
                )
                tui.submit(task.prompt)
                tui.task(evidence, task)
                tui.wait_for(
                    lambda _: (
                        _smart_router_band_hidden(tui)
                        and _smart_router_hint_shown(tui, enabled=True)
                    ),
                    "the Smart Router band hidden after the launch turn, with "
                    "smart router ON left in the prompt hint row",
                )
                for enabled in (False, True):
                    tui.submit(f"/smart-router {'on' if enabled else 'off'}")
                    tui.wait_for(
                        lambda _, enabled=enabled: _smart_router_band_shown(
                            tui, enabled=enabled, first_prompt=first_prompt_routed
                        ),
                        f"the {'ON' if enabled else 'OFF'} Smart Router band",
                    )
                    state = "ON" if enabled else "OFF"
                    # The hint row flips with the toggle itself, before any turn.
                    tui.wait_for(
                        lambda _, enabled=enabled: _smart_router_hint_shown(tui, enabled=enabled),
                        f"smart router {state} in the prompt hint row after the toggle",
                    )
                    # The band covers the toggle's first turn and is gone by the second;
                    # the hint row keeps the state.
                    follow_up = _file_task(session, CLAUDE)
                    tui.submit(follow_up.prompt)
                    tui.task(evidence, follow_up)
                    tui.wait_for(
                        lambda _, enabled=enabled: (
                            _smart_router_band_hidden(tui)
                            and _smart_router_hint_shown(tui, enabled=enabled)
                        ),
                        f"the {state} Smart Router band hidden by the second turn, with "
                        f"smart router {state} left in the prompt hint row",
                    )
                tui.exit_normally()

    @pytest.mark.parametrize("agent", AGENTS)
    def test_agent_completes_real_first_prompt_file_task_without_model_override(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.no_model_override[agent]
        assert case.launch_args == (agent,)
        assert case.task.value not in case.task.prompt
        assert case.observation.turn is not None
        assert case.task.value in case.observation.turn.answer

    @pytest.mark.parametrize("agent", AGENTS)
    def test_managed_smart_routing_orchestrates_without_routing_the_first_prompt(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.no_model_override[agent]
        assert not [request for request in case.requests if request.path == ROUTING_PATH]
        assert ORCHESTRATOR_CONTEXT.encode() in case.inference_request.body

    @pytest.mark.parametrize("agent", AGENTS)
    def test_admin_default_model_serves_the_first_prompt(self, completed_smart_routing_runs, agent):
        case = completed_smart_routing_runs.no_model_override[agent]
        expected = completed_smart_routing_runs.defaults[agent]
        assert canonical_model(case.inference_request.payload["model"]) == canonical_model(expected)
        assert case.inference_response.status_code == 200
        result = case.observation.assert_applied(
            case.task,
            completed_smart_routing_runs.supported[agent],
            expected=expected,
        )
        assert case.task.value in result["answer"]

    @pytest.mark.parametrize("agent", AGENTS)
    def test_with_model_override_bypasses_router_and_uses_requested_model(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.with_model_override[agent]
        expected = completed_smart_routing_runs.overrides[agent]
        assert case.launch_args == (agent, "--model", expected)
        assert not [request for request in case.requests if request.path == ROUTING_PATH]
        inference_model = case.inference_request.payload["model"]
        if agent == CLAUDE:
            assert inference_model == expected
        else:
            assert canonical_model(inference_model) == canonical_model(expected)
        assert case.inference_response.status_code == 200
        case.observation.assert_applied(
            case.task,
            completed_smart_routing_runs.supported[agent],
            expected=expected,
        )

    @pytest.mark.skip(reason="Requires a separate read-only workspace with routing disabled")
    def test_routing_disabled_fresh_sessions_use_defaults_without_router_decisions(self):
        """Covered when a second, preconfigured routing-disabled CUJ workspace is available."""
