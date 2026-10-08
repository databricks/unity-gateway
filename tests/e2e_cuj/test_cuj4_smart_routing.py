"""Smart-routing CUJ: execute once, then report each contract assertion separately."""

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
)

from .base import BaseCujTest
from .helpers.constants import CLAUDE, CODEX, INFERENCE_PATHS, CodingAgent
from .helpers.evidence import (
    SessionEvidence,
    SessionObservation,
    canonical_model,
    claude_file_task,
)
from .helpers.terminal import Terminal
from .helpers.tui_request_recorder import RecordedRequest, RecordedResponse
from .helpers.workspace import Workspace

CUJ_NAME = "CUJ 4 · Smart routing"

ROUTING_PATH = "/ai-gateway/routing/v1/routes:select"
AGENTS = (CLAUDE, CODEX)
ORCHESTRATOR_CONTEXT = "Smart Router Orchestrator is on for this session."


@dataclass(frozen=True)
class SessionCase:
    agent: str
    launch_args: tuple[str, ...]
    task: FileTask
    observation: SessionObservation
    requests: tuple[RecordedRequest, ...]
    inference_request: RecordedRequest
    inference_response: RecordedResponse
    route_request: RecordedRequest | None = None
    route_response: RecordedResponse | None = None

    @property
    def selected_model(self):
        assert self.route_response is not None
        selections = self.route_response.payload["route_selection"]
        assert len(selections) == 1
        return canonical_model(selections[0]["route_option"]["model"])


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
        route_request = next(
            request
            for request in requests
            if request.method == "POST" and request.path == ROUTING_PATH
        )
        route_response = recorder.response_for(route_request)
        inference_request = next(
            request
            for request in requests
            if request.method == "POST" and request.path == INFERENCE_PATHS[agent]
        )
        no_model_override[agent] = SessionCase(
            agent=agent,
            launch_args=(agent,),
            task=task,
            observation=observation,
            requests=requests,
            route_request=route_request,
            route_response=route_response,
            inference_request=inference_request,
            inference_response=recorder.response_for(inference_request),
        )

        task = _file_task(session, agent)
        launch_args = (agent, "--model", overrides[agent])
        observation, requests = _run_session(session, recorder, agent, task, launch_args)
        inference_request = next(
            request
            for request in requests
            if request.method == "POST" and request.path == INFERENCE_PATHS[agent]
        )
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

    @pytest.mark.parametrize(
        "SMART_ROUTER_CONFIG_VERSION, first_prompt_routed, orchestrator_enabled",
        [
            (FIRST_PROMPT_AND_SUBAGENT_NO_ORCH_V0, True, False),
            (SUBAGENT_ONLY_V0, False, False),
            (SUBAGENT_ONLY_V1, False, False),
            (SUBAGENT_ORCH_V0, False, True),
        ],
    )
    def test_smart_router_config_version(
        self, cuj, SMART_ROUTER_CONFIG_VERSION, first_prompt_routed, orchestrator_enabled
    ):
        """Scenario: launch both agents with a preset, then explicitly request a subagent.

        Expected: first-prompt routing and orchestrator context match the preset;
        one routed native child completes the delegated task.
        """
        session, workspace, recorder = cuj
        previous = session.env.get("SMART_ROUTER_CONFIG_VERSION")
        session.env["SMART_ROUTER_CONFIG_VERSION"] = SMART_ROUTER_CONFIG_VERSION
        try:
            configs = _assert_published_config_matches_expectations(workspace.config())
            recorder.configure_session(session, ["configure", "--disable-databricks-ai-tools"])
            for agent in AGENTS:
                supported = workspace.model_ids(agent)
                evidence = SessionEvidence(session.home, agent)
                existing_sessions = set(agent_sessions(session, agent))
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
                    inference = next(
                        request
                        for request in requests
                        if request.method == "POST" and request.path == INFERENCE_PATHS[agent]
                    )
                    assert recorder.response_for(inference).status_code == 200
                    assert canonical_model(inference.payload["model"]) == canonical_model(
                        expected_model
                    )
                    evidence.assert_applied(task, supported, expected=expected_model)
                    assert (
                        ORCHESTRATOR_CONTEXT.encode() in inference.body
                    ) == orchestrator_enabled, agent
                    assert not any(
                        is_child_session(agent, path, records)
                        for path, records in agent_sessions(session, agent).items()
                        if path not in existing_sessions
                    ), agent

                    child_task = FileTask(session)
                    child_task.prompt = child_task.delegate_prompt
                    decisions_path = (
                        session.home / ".ucode" / f"{agent}-smart-routing-decisions.jsonl"
                    )
                    decision_count = len(read_jsonl(decisions_path))
                    checkpoint = recorder.checkpoint()
                    tui.submit(child_task.prompt)
                    tui.task(evidence, child_task)
                    tui.wait_for(
                        lambda screen, task=child_task, agent=agent: task.completed(
                            session, agent, child=True
                        ),
                        "completed native subagent file task",
                        timeout=240,
                    )
                    children = {
                        path: records
                        for path, records in agent_sessions(session, agent).items()
                        if path not in existing_sessions and is_child_session(agent, path, records)
                    }
                    assert len(children) == 1, (agent, children.keys())
                    assert any(
                        child_task.value in answer
                        for records in children.values()
                        for answer in assistant_answers(agent, records)
                    ), agent
                    decisions = read_jsonl(decisions_path)[decision_count:]
                    assert_subagent_routed(
                        session,
                        agent,
                        child_task,
                        decision_ids={decision["decision_id"] for decision in decisions},
                    )
                    requests = recorder.requests_after(checkpoint)
                    routes = [request for request in requests if request.path == ROUTING_PATH]
                    assert len(routes) == 1, (agent, routes)
                    assert child_task.filename in routes[0].payload["task"]["prompt"]
                    response = recorder.response_for(routes[0])
                    assert response.status_code == 200
                    selections = response.payload["route_selection"]
                    assert len(selections) == 1
                    inference = next(
                        request
                        for request in requests
                        if request.method == "POST"
                        and request.path == INFERENCE_PATHS[agent]
                        and request.sequence > routes[0].sequence
                    )
                    assert recorder.response_for(inference).status_code == 200
                    assert canonical_model(inference.payload["model"]) == canonical_model(
                        selections[0]["route_option"]["model"]
                    )
                    tui.exit_normally()
        finally:
            if previous is None:
                session.env.pop("SMART_ROUTER_CONFIG_VERSION", None)
            else:
                session.env["SMART_ROUTER_CONFIG_VERSION"] = previous

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
    def test_router_decision_exists_and_is_correlated_to_the_prompt(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.no_model_override[agent]
        decisions = [request for request in case.requests if request.path == ROUTING_PATH]
        assert decisions == [case.route_request]
        assert case.route_request.payload["task"]["prompt"] == case.task.prompt
        assert case.route_request.payload["route_selector"]["router_name"]
        assert case.route_response.status_code == 200

    @pytest.mark.parametrize("agent", AGENTS)
    def test_selected_model_is_used_for_inference_and_the_task_completes(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.no_model_override[agent]
        inference_model = case.inference_request.payload["model"]
        if agent == CLAUDE:
            assert inference_model == case.selected_model
        else:
            assert canonical_model(inference_model) == case.selected_model
        assert case.inference_response.status_code == 200
        result = case.observation.assert_applied(
            case.task,
            completed_smart_routing_runs.supported[agent],
            expected=case.selected_model,
        )
        assert case.task.value in result["answer"]

    @pytest.mark.parametrize("agent", AGENTS)
    def test_selected_model_is_a_supported_system_ai_target_in_the_live_router_contract(
        self, completed_smart_routing_runs, agent
    ):
        case = completed_smart_routing_runs.no_model_override[agent]
        live_supported = {
            canonical_model(model) for model in completed_smart_routing_runs.supported[agent]
        }
        options = case.route_request.payload["route_options"]
        offered = {canonical_model(option["model"]) for option in options}
        assert options and all(option["harness"] == agent for option in options)
        assert offered <= live_supported
        assert case.selected_model in offered
        assert case.selected_model.startswith("system.ai.")

    def test_routing_assertions_accept_each_agents_independent_supported_selection(
        self, completed_smart_routing_runs
    ):
        # There is deliberately no expected winner and no cross-agent comparison.
        for agent, case in completed_smart_routing_runs.no_model_override.items():
            response_model = canonical_model(
                case.route_response.payload["route_selection"][0]["route_option"]["model"]
            )
            assert case.selected_model == response_model
            assert response_model in {
                canonical_model(model) for model in completed_smart_routing_runs.supported[agent]
            }

    @pytest.mark.parametrize(
        "agent",
        [
            pytest.param(
                CLAUDE,
                marks=pytest.mark.skip(
                    reason="TODO: Fix Claude model override precedence over managed defaults"
                ),
            ),
            CODEX,
        ],
    )
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
