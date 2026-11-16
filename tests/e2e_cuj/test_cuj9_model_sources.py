"""CUJ9: mixed model sources, native discovery, inference, and Claude-only routing."""

import json
import shutil

import pytest

from tests.integration.utils.agents import claude, codex, copilot
from tests.integration.utils.constants import COPILOT_HEADLESS_FLAGS
from tests.integration.utils.evidence import (
    FileTask,
    agent_sessions,
    assert_completed_task_model,
    assert_no_session_registry_left,
    assert_no_terminal_api_error,
    assert_subagent_routed,
    read_jsonl,
)
from tests.integration.utils.model_discovery import assert_picker_inventory
from tests.integration.utils.provider_catalog import (
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    parse_anthropic_provider_page,
    parse_codex_provider_catalog,
)
from tests.integration.utils.terminal import AgentTerminal

from .base import BaseCujTest
from .helpers.constants import (
    CLAUDE,
    CLAUDE_SONNET_MODEL_SERVICE,
    CODEX,
    CODEX_LUNA_MODEL_SERVICE,
    COPILOT,
    SOURCE_ANTHROPIC_PROVIDER,
    SOURCE_ANTHROPIC_TARGET,
    SOURCE_CLAUDE_ALIAS,
    SOURCE_CLAUDE_PROVIDER_MODEL,
    SOURCE_CODEX_ALIAS,
    SOURCE_CODEX_PROVIDER_MODEL,
    SOURCE_COPILOT_DEFAULT_MODEL,
    SOURCE_OPENAI_PROVIDER,
    SOURCE_OPENAI_TARGET,
    SOURCE_SCHEMA,
)
from .helpers.evidence import (
    SessionEvidence,
    _request_contains_task,
    assert_claude_headless_model,
    assert_inference_evidence,
    assert_served,
    claude_file_task,
    message_text,
    served_inference_request,
)
from .helpers.terminal import Terminal

CUJ_NAME = "CUJ 9 · Model sources"
pytestmark = [pytest.mark.live, pytest.mark.managed, pytest.mark.workspace_isolated]

ROUTING_PATH = "/ai-gateway/routing/v1/routes:select"
CLAUDE_MODELS = {
    SOURCE_CLAUDE_PROVIDER_MODEL: "Claude Haiku 5.5",
    CLAUDE_SONNET_MODEL_SERVICE: "Claude_Sonnet",
    SOURCE_CLAUDE_ALIAS: "Claude_Decoy",
    SOURCE_CODEX_ALIAS: "Codex_Decoy",
}
CODEX_MODELS = {
    SOURCE_CODEX_PROVIDER_MODEL: "GPT 6 Luna",
    CODEX_LUNA_MODEL_SERVICE: "Gpt_Luna",
    SOURCE_CODEX_ALIAS: "Codex_Decoy",
}
COPILOT_MODELS = {
    SOURCE_CLAUDE_PROVIDER_MODEL: "dbx-anthropic",
    SOURCE_COPILOT_DEFAULT_MODEL: "dbx-responses",
    SOURCE_CODEX_PROVIDER_MODEL: "dbx-responses",
}


@pytest.fixture(scope="class")
def source_contract(cuj):
    """Validate the workspace's published sources and independent upstream catalogs."""
    _, workspace, _ = cuj
    published = workspace.config()
    assert published["default_agent"] == "CODING_AGENT_CLAUDE_CODE", published
    configs = workspace.agent_configs(published)
    sources_and_defaults = {
        "CODING_AGENT_CLAUDE_CODE": (
            [SOURCE_CLAUDE_PROVIDER_MODEL, CLAUDE_SONNET_MODEL_SERVICE, f"{SOURCE_SCHEMA}.*"],
            CLAUDE_SONNET_MODEL_SERVICE,
            True,
        ),
        "CODING_AGENT_CODEX": (
            [SOURCE_CODEX_PROVIDER_MODEL, CODEX_LUNA_MODEL_SERVICE, f"{SOURCE_SCHEMA}.*"],
            CODEX_LUNA_MODEL_SERVICE,
            False,
        ),
        "CODING_AGENT_GITHUB_COPILOT": (
            [
                SOURCE_CLAUDE_PROVIDER_MODEL,
                SOURCE_COPILOT_DEFAULT_MODEL,
                f"{SOURCE_OPENAI_PROVIDER}.*",
            ],
            SOURCE_COPILOT_DEFAULT_MODEL,
            False,
        ),
    }
    assert configs.keys() == sources_and_defaults.keys(), configs
    for agent, (sources, default, routing) in sources_and_defaults.items():
        config = configs[agent]
        assert config["models"]["sources"] == sources, (agent, config)
        assert config["default_models"]["default_model"] == default, (agent, config)
        assert config["smart_routing"]["enabled"] is routing, (agent, config)
        assert config["tracing"]["enabled"] is False, (agent, config)
    assert configs["CODING_AGENT_GITHUB_COPILOT"]["vscode_integration"]["enabled"] is False
    assert shutil.which(COPILOT), "Install the pinned Copilot CLI before running CUJ9"
    for provider, target in (
        (SOURCE_ANTHROPIC_PROVIDER, SOURCE_ANTHROPIC_TARGET),
        (SOURCE_OPENAI_PROVIDER, SOURCE_OPENAI_TARGET),
    ):
        service = workspace.client.ai_gateway.get_model_provider_service(
            f"model-provider-services/{provider}"
        )
        assert service.config and service.config.allow_all_targets is not True, provider
        assert [item.model for item in service.config.targets or []] == [target], provider
    for model in (
        CLAUDE_SONNET_MODEL_SERVICE,
        CODEX_LUNA_MODEL_SERVICE,
        SOURCE_COPILOT_DEFAULT_MODEL,
    ):
        service = workspace.client.ai_gateway.get_model_service(f"model-services/{model}")
        assert service.name == f"model-services/{model}", model
    headers = {MODEL_SERVICE_PARENT_SCHEMA_HEADER: SOURCE_SCHEMA}
    payload = workspace.client.api_client.do(
        "GET", "/ai-gateway/anthropic/v1/models", headers=headers, query={"limit": "1000"}
    )
    page = parse_anthropic_provider_page(payload)
    assert not page.has_more, "CUJ9's two-model schema unexpectedly needs pagination"
    assert {model for model, _ in page.models} == {
        claude.discovery_model_id(SOURCE_CLAUDE_ALIAS),
        claude.discovery_model_id(SOURCE_CODEX_ALIAS),
    }
    payload = workspace.client.api_client.do("GET", "/ai-gateway/codex/v1/models", headers=headers)
    assert set(parse_codex_provider_catalog(payload)) == {SOURCE_CODEX_ALIAS}


@pytest.fixture(autouse=True)
def clean_source_case(cuj, source_contract):
    yield
    session, _, _ = cuj
    session.revert_machine_wide("sources-revert", "CUJ9 left machine-wide agent settings")


def _assert_no_routes(recorder, checkpoint):
    assert not [
        request for request in recorder.requests_after(checkpoint) if request.path == ROUTING_PATH
    ], "A source inference task unexpectedly invoked Smart Router"


def _assert_copilot_inference(recorder, checkpoint, task, model):
    """The shared CUJ inference helper only knows Claude/Codex endpoint paths."""
    anthropic = COPILOT_MODELS[model] == "dbx-anthropic"
    path = "/ai-gateway/anthropic/v1/messages" if anthropic else "/ai-gateway/mlflow/v1/responses"
    requests = [
        request
        for request in recorder.requests_after(checkpoint)
        if request.method == "POST"
        and request.path == path
        and _request_contains_task(request, CLAUDE if anthropic else CODEX, task)
    ]
    assert requests, f"No Copilot inference at {path} contained the exact task prompt"
    for request in requests:
        assert_served(recorder, request, model)


def _assert_copilot_selection(session, task, model):
    records = [
        records
        for records in agent_sessions(session, COPILOT).values()
        if any(task.value in answer for answer in copilot.assistant_answers(records))
    ]
    assert len(records) == 1, "Expected exactly one Copilot session completing this file task"
    assert copilot.selected_models(records[0]) == [f"{COPILOT_MODELS[model]}/{model}"]


class TestModelSources(BaseCujTest):
    WORKSPACE_URL = "https://dbc-93fdd3cb-4b13.cloud.databricks.com"

    @pytest.mark.claude
    @pytest.mark.tui
    def test_sources_claude_discovers_mixed_models_and_uses_custom_default(self, cuj):
        """Scenario: configure Claude, inspect /model, and complete a default-model file task.

        Expected: the exact mixed-source picker includes the MPS deep link, custom service,
        and both schema-wildcard models. The custom default serves the completed task.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = claude_file_task(session)
        task.prompt += " Do not delegate."
        evidence = SessionEvidence(session.home, CLAUDE)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "sources-claude-discovery", [CLAUDE]) as tui:
            tui.boot()
            screen = tui.open_model_picker(
                model_visible=lambda text: all(
                    claude.model_in_picker(text, model, label)
                    for model, label in CLAUDE_MODELS.items()
                )
            )
            assert_picker_inventory(screen, CLAUDE, dict[str, str | None](CLAUDE_MODELS))
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        task.assert_completed(session, CLAUDE)
        assert_inference_evidence(recorder, checkpoint, CLAUDE, task, CLAUDE_SONNET_MODEL_SERVICE)
        _assert_no_routes(recorder, checkpoint)

    @pytest.mark.codex
    @pytest.mark.tui
    def test_sources_codex_discovers_mixed_models_and_uses_custom_default(self, cuj):
        """Scenario: configure Codex, inspect model/list and /model, then complete a file task.

        Expected: both inventories contain exactly the MPS deep link, custom service, and
        compatible schema-wildcard model. The custom default serves inference without routing.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
        assert len(models) == len(set(models)) and set(models) == set(CODEX_MODELS), models
        task = FileTask(session)
        evidence = SessionEvidence(session.home, CODEX)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "sources-codex-discovery", [CODEX]) as tui:
            tui.boot()
            screen = tui.open_codex_model_picker(
                model_visible=lambda text: all(
                    codex.model_in_picker(text, model, label)
                    for model, label in CODEX_MODELS.items()
                )
            )
            assert_picker_inventory(screen, CODEX, dict[str, str | None](CODEX_MODELS))
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        assert_completed_task_model(session, CODEX, task.value, CODEX_LUNA_MODEL_SERVICE)
        assert_inference_evidence(recorder, checkpoint, CODEX, task, CODEX_LUNA_MODEL_SERVICE)
        _assert_no_routes(recorder, checkpoint)

    @pytest.mark.copilot
    @pytest.mark.tui
    def test_sources_copilot_discovers_mixed_models_and_uses_service_default(self, cuj):
        """Scenario: configure Copilot, inspect its live registry, and complete a TUI file task.

        Expected: the exact registry retains the Anthropic deep link, hosted service, and
        provider-wildcard GPT on their native wires. The hosted default completes inference,
        no routing occurs, and the credential-bearing registry is removed on exit.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        command = [
            str(session.binary),
            COPILOT,
            "--allow-all-tools",
            "--no-auto-update",
            "--disable-builtin-mcps",
        ]
        with AgentTerminal(session, COPILOT, command, "sources-copilot-discovery") as tui:
            tui.boot()
            paths = list(session.home.glob(".copilot/ug-providers-*.json"))
            assert len(paths) == 1, paths
            registry = json.loads(paths[0].read_text())
            models = [
                {key: row[key] for key in ("id", "wireModel", "provider")}
                for row in registry["models"]
            ]
            session.record("sources-copilot-models.json", models)
            assert len(models) == len(COPILOT_MODELS), models
            assert {row["id"]: row["provider"] for row in models} == COPILOT_MODELS, models
            assert all(row["wireModel"] == row["id"] for row in models), models
            tui.submit(task.prompt)

            def completed(screen):
                assert_no_terminal_api_error(screen)
                assert "Do you want to proceed?" not in screen, screen
                return task.completed(session, COPILOT)

            tui.wait_for(completed, "completed native Copilot file task", timeout=240)
            tui.exit_normally()
        task.assert_completed(session, COPILOT)
        assert_completed_task_model(session, COPILOT, task.value, SOURCE_COPILOT_DEFAULT_MODEL)
        _assert_copilot_selection(session, task, SOURCE_COPILOT_DEFAULT_MODEL)
        _assert_copilot_inference(recorder, checkpoint, task, SOURCE_COPILOT_DEFAULT_MODEL)
        _assert_no_routes(recorder, checkpoint)
        assert_no_session_registry_left(session)

    @pytest.mark.claude
    @pytest.mark.parametrize("model", CLAUDE_MODELS)
    def test_sources_claude_inference_on_each_source_model(self, cuj, model):
        """Scenario: configure and run a Claude print file task on each mixed-source model.

        Expected: the native final answer contains the hidden value, reports output tokens,
        and the exact requested provider or service receives successful gateway inference.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = claude_file_task(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        result = session.run(
            CLAUDE,
            "--",
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            "--model",
            claude.discovery_model_id(model),
            timeout=240,
        )
        task.assert_headless_answer(CLAUDE, result)
        assert_claude_headless_model(result, claude.discovery_model_id(model))
        assert_inference_evidence(recorder, checkpoint, CLAUDE, task, model)
        _assert_no_routes(recorder, checkpoint)

    @pytest.mark.codex
    @pytest.mark.parametrize("model", CODEX_MODELS)
    def test_sources_codex_inference_on_each_source_model(self, cuj, model):
        """Scenario: configure and run a Codex exec file task on each mixed-source model.

        Expected: every deep link, custom service, and schema-wildcard selection produces
        a completed native answer and successful exact-model gateway inference without routing.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        result = session.run(
            CODEX,
            "--",
            "exec",
            "--skip-git-repo-check",
            "--json",
            task.prompt,
            "--model",
            model,
            timeout=240,
        )
        task.assert_headless_answer(CODEX, result)
        assert_completed_task_model(session, CODEX, task.value, model)
        assert_inference_evidence(recorder, checkpoint, CODEX, task, model)
        _assert_no_routes(recorder, checkpoint)

    @pytest.mark.copilot
    @pytest.mark.parametrize("model", COPILOT_MODELS)
    def test_sources_copilot_inference_on_each_source_model(self, cuj, model):
        """Scenario: configure and run a Copilot headless file task on every registered source.

        Expected: the MPS deep link, hosted service, and provider-wildcard model each serve
        a completed native answer on the exact model, without routing or leftover registries.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        result = session.run(
            COPILOT, "--model", model, "-p", task.prompt, *COPILOT_HEADLESS_FLAGS, timeout=240
        )
        task.assert_headless_answer(COPILOT, result)
        assert_completed_task_model(session, COPILOT, task.value, model)
        _assert_copilot_selection(session, task, model)
        _assert_copilot_inference(recorder, checkpoint, task, model)
        _assert_no_routes(recorder, checkpoint)
        assert_no_session_registry_left(session)

    @pytest.mark.claude
    @pytest.mark.tui
    def test_sources_claude_routes_a_native_child_within_expanded_sources(self, cuj):
        """Scenario: configure the Claude-only managed router and delegate a real TUI file task.

        Expected: one successful routing decision offers only expanded source models, its
        selected model serves the child request, and the native child and parent both finish.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        task.prompt = (
            f"Delegate to exactly one subagent: use the Read tool to read "
            f"{session.cwd / task.filename} and return its contents. "
            "Do not read it yourself. Wait for the subagent and reply with only its value."
        )
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(
            session,
            "sources-claude-routing",
            [CLAUDE, "--", "--disallowedTools", "ScheduleWakeup,CronCreate"],
        ) as tui:
            tui.boot(timeout=150)
            tui.submit(task.prompt)
            tui.wait_until(
                lambda: (
                    task.completed(session, CLAUDE, child=True) and task.completed(session, CLAUDE)
                ),
                "completed native routed child and parent file tasks",
            )
            tui.wait_for_background_tasks()
            tui.exit_normally()
        task.assert_completed(session, CLAUDE, child=True)
        task.assert_completed(session, CLAUDE)
        requests = recorder.requests_after(checkpoint)
        routes = [request for request in requests if request.path == ROUTING_PATH]
        assert len(routes) == 1, routes
        route = routes[0]
        route_prompt = route.payload["task"]["prompt"]
        assert task.filename in route_prompt, route_prompt
        options = route.payload["route_options"]
        assert options and all(option["harness"] == CLAUDE for option in options), options
        offered = {option["model"] for option in options}
        expected_options = {claude.discovery_model_id(model) for model in CLAUDE_MODELS}
        assert offered <= expected_options, offered
        response = recorder.response_for(route)
        assert response.status_code == 200, response.status_code
        selections = response.payload["route_selection"]
        assert len(selections) == 1, selections
        selected = selections[0]["route_option"]["model"]
        assert selected in offered, selected
        child_requests = [
            request
            for request in requests
            if request.sequence > route.sequence
            and request.method == "POST"
            and request.path == "/ai-gateway/anthropic/v1/messages"
            and isinstance(request.payload.get("tools"), list)
            and request.payload["tools"]
            and any(
                entry.get("role") == "user"
                and message_text(entry.get("content", "")).rstrip("\n") == route_prompt.rstrip("\n")
                for entry in request.payload.get("messages", [])
            )
        ]
        assert child_requests, "No inference request matched the routed child's assignment"
        for request in child_requests:
            served = served_inference_request(recorder, child_requests, request, CLAUDE)
            assert_served(recorder, served, selected)
        decisions = read_jsonl(session.home / ".ucode/claude-smart-routing-decisions.jsonl")
        matching = [decision for decision in decisions if decision.get("task_name") == route_prompt]
        assert len(matching) == 1, decisions
        assert matching[0]["router_model"] == selected, matching
        assert_subagent_routed(session, CLAUDE, task, decision_ids={matching[0]["decision_id"]})
