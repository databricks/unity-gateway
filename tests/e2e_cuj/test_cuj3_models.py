"""Managed catalog discovery, default launches, and explicit model selection."""

import json

import pytest

from tests.integration.utils.agents import claude, codex
from tests.integration.utils.evidence import FileTask, assert_completed_task_model
from tests.integration.utils.model_discovery import assert_picker_inventory
from tests.integration.utils.provider_catalog import (
    MODEL_SERVICE_PARENT_SCHEMA_HEADER,
    parse_anthropic_provider_page,
    parse_codex_provider_catalog,
)

from .base import BaseCujTest
from .catalog_discovery_expectations import (
    CLAUDE_DECOY,
    CLAUDE_DEFAULT,
    CLAUDE_MODELS,
    CODEX_DECOY,
    CODEX_DEFAULT,
    CODEX_MODELS,
    GEMINI_MODEL,
    MODEL_SCHEMA,
    OTHER_MODEL_SCHEMA,
)
from .helpers.constants import CLAUDE, CODEX, INFERENCE_PATHS
from .helpers.evidence import (
    SessionEvidence,
    assert_served,
    claude_file_task,
    message_text,
    served_inference_request,
)
from .helpers.terminal import Terminal

CUJ_NAME = "CUJ 3 · UC model discovery"

pytestmark = [pytest.mark.managed, pytest.mark.catalog_discovery, pytest.mark.workspace_isolated]


def _catalog_display_names(workspace, agent, schema):
    """Read a scoped catalog; Workspace.model_ids only supports system.ai."""
    headers = {MODEL_SERVICE_PARENT_SCHEMA_HEADER: schema}
    if agent == CODEX:
        payload = workspace.client.api_client.do(
            "GET", "/ai-gateway/codex/v1/models", headers=headers
        )
        models = parse_codex_provider_catalog(payload)
        return {
            entry["slug"]: entry.get("display_name") or entry["slug"]
            for entry in payload["models"]
            if entry["slug"] in models
        }
    assert agent == CLAUDE, agent
    headers["Anthropic-Version"] = "2023-06-01"
    models, seen, cursor = {}, set(), None
    for _page in range(20):
        query = {"limit": "1000"}
        if cursor:
            query["after_id"] = cursor
        payload = workspace.client.api_client.do(
            "GET", "/ai-gateway/anthropic/v1/models", headers=headers, query=query
        )
        page = parse_anthropic_provider_page(payload)
        for model, display_name in page.models:
            assert model not in models, f"Repeated catalog model: {model}"
            models[model] = display_name
        if not page.has_more:
            return models
        cursor = page.last_id
        assert cursor and cursor not in seen, "Repeated catalog pagination cursor"
        seen.add(cursor)
    raise AssertionError("Anthropic catalog exceeded 20 pages")


def _assert_claude_headless_model(result, expected):
    final = None
    for line in result.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            final = payload
    assert final is not None and not final.get("is_error"), result.stdout
    usage = final["modelUsage"]
    assert set(usage) == {expected}, {"expected": expected, "observed": sorted(usage)}
    assert usage[expected]["outputTokens"] > 0, usage


def _request_contains_task(request, agent, task):
    field = "messages" if agent == CLAUDE else "input"
    entries = request.payload.get(field)
    if not isinstance(entries, (list, str)):
        return False
    if isinstance(entries, str):
        return entries == task.prompt
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content", "")
        if isinstance(content, str) and content == task.prompt:
            return True
        if isinstance(content, list) and any(
            message_text([part]) == task.prompt for part in content if isinstance(part, dict)
        ):
            return True
    return False


def _assert_inference_evidence(recorder, checkpoint, agent, task, expected):
    expected_wire_model = claude.discovery_model_id(expected) if agent == CLAUDE else expected
    requests = recorder.requests_after(checkpoint)
    inference_requests = [
        request
        for request in requests
        if request.method == "POST" and request.path == INFERENCE_PATHS[agent]
    ]
    assert inference_requests, {
        "agent": agent,
        "path": INFERENCE_PATHS[agent],
        "requests": [(request.method, request.path) for request in requests],
    }
    task_requests = [
        request for request in inference_requests if _request_contains_task(request, agent, task)
    ]
    assert task_requests, "No inference request contained the submitted task prompt"
    for request in task_requests:
        served = served_inference_request(recorder, task_requests, request, agent)
        assert_served(recorder, served, expected_wire_model)


@pytest.fixture(autouse=True)
def _revert_catalog_test_state(cuj):
    yield
    session, _, _ = cuj
    session.revert_machine_wide(
        "catalog-discovery-cleanup-revert", "Catalog CUJ teardown left machine-wide agent settings"
    )


class TestCatalogDiscovery(BaseCujTest):
    WORKSPACE_URL = "https://dbc-bbdd5508-648e.cloud.databricks.com"

    @pytest.mark.claude
    @pytest.mark.tui
    def test_catalog_discovery_claude_picker_preserves_default(self, cuj):
        """Scenario: configure, launch ug claude, inspect its picker, then submit a task.

        Expected: only Sonnet/Haiku/Kimi appear; Gemini and decoys are absent.
        Dismissing the picker without a selection preserves Sonnet for the completed task.
        """
        session, workspace, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])

        parent_catalog = _catalog_display_names(workspace, CLAUDE, MODEL_SCHEMA)
        decoy_catalog = _catalog_display_names(workspace, CLAUDE, OTHER_MODEL_SCHEMA)
        assert set(parent_catalog) == {
            claude.discovery_model_id(model) for model in CLAUDE_MODELS
        }, parent_catalog
        assert set(decoy_catalog) == {claude.discovery_model_id(CLAUDE_DECOY)}, decoy_catalog
        default_display_name = parent_catalog[claude.discovery_model_id(CLAUDE_DEFAULT)]

        task = claude_file_task(session)
        evidence = SessionEvidence(session.home, CLAUDE)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "catalog-discovery-claude-picker", [CLAUDE]) as tui:
            tui.boot()
            picker_screen = tui.open_model_picker(
                model_visible=lambda screen: all(
                    claude.model_in_picker(screen, model, parent_catalog[model])
                    for model in parent_catalog
                )
            )
            assert_picker_inventory(picker_screen, CLAUDE, parent_catalog)
            for excluded in (GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY):
                assert excluded not in picker_screen, picker_screen
                assert excluded.rsplit(".", 1)[-1] not in picker_screen, picker_screen
            assert f"{default_display_name} · API Usage Billing" in tui.visible, tui.visible
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        task.assert_completed(session, CLAUDE)
        _assert_inference_evidence(recorder, checkpoint, CLAUDE, task, CLAUDE_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.tui
    def test_catalog_discovery_codex_picker_preserves_default(self, cuj):
        """Scenario: configure, query Codex model/list, inspect its TUI picker, then submit a task.

        Expected: model/list and the picker expose only GPT Luna/Kimi, not Gemini or decoys.
        Dismissing the picker preserves GPT Luna for the completed task's selected model.
        """
        session, workspace, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])

        parent_catalog = _catalog_display_names(workspace, CODEX, MODEL_SCHEMA)
        decoy_catalog = _catalog_display_names(workspace, CODEX, OTHER_MODEL_SCHEMA)
        assert set(parent_catalog) == CODEX_MODELS, parent_catalog
        assert set(decoy_catalog) == {CODEX_DECOY}, decoy_catalog

        models = session.codex_model_ids(["app-server", "--listen", "stdio://"])
        assert len(models) == len(set(models)), models
        assert set(models) == CODEX_MODELS, models
        display_names = parent_catalog

        task = FileTask(session)
        evidence = SessionEvidence(session.home, CODEX)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "catalog-discovery-codex-picker", [CODEX]) as tui:
            tui.boot()
            picker_screen = tui.open_codex_model_picker(
                model_visible=lambda screen: all(
                    codex.model_in_picker(screen, model, display_names[model])
                    for model in parent_catalog
                )
            )
            assert_picker_inventory(picker_screen, CODEX, display_names)
            for excluded in (GEMINI_MODEL, CLAUDE_DECOY, CODEX_DECOY):
                assert excluded not in picker_screen, picker_screen
                assert excluded.rsplit(".", 1)[-1] not in picker_screen, picker_screen
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        assert_completed_task_model(session, CODEX, task.value, CODEX_DEFAULT)
        _assert_inference_evidence(recorder, checkpoint, CODEX, task, CODEX_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.tui
    def test_catalog_discovery_bare_ug_uses_claude_default(self, cuj):
        """Scenario: configure the managed workspace, launch bare ug, and submit its first task.

        Expected: Claude completes the task on Sonnet without an agent or model override.
        """
        session, workspace, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        default_display_name = _catalog_display_names(workspace, CLAUDE, MODEL_SCHEMA)[
            claude.discovery_model_id(CLAUDE_DEFAULT)
        ]
        task = claude_file_task(session)
        evidence = SessionEvidence(session.home, CLAUDE)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "catalog-discovery-bare-ug-default", [], agent=CLAUDE) as tui:
            tui.boot()
            assert f"{default_display_name} · API Usage Billing" in tui.visible, tui.visible
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        task.assert_completed(session, CLAUDE)
        _assert_inference_evidence(recorder, checkpoint, CLAUDE, task, CLAUDE_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.tui
    def test_catalog_discovery_claude_tui_uses_default(self, cuj):
        """Scenario: configure, launch ug claude, and submit its first task without opening a picker.

        Expected: Claude completes the task on the configured Sonnet default without overrides.
        """
        session, workspace, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        default_display_name = _catalog_display_names(workspace, CLAUDE, MODEL_SCHEMA)[
            claude.discovery_model_id(CLAUDE_DEFAULT)
        ]
        task = claude_file_task(session)
        evidence = SessionEvidence(session.home, CLAUDE)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "catalog-discovery-claude-tui-default", [CLAUDE]) as tui:
            tui.boot()
            assert f"{default_display_name} · API Usage Billing" in tui.visible, tui.visible
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        task.assert_completed(session, CLAUDE)
        _assert_inference_evidence(recorder, checkpoint, CLAUDE, task, CLAUDE_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.tui
    def test_catalog_discovery_codex_tui_uses_default(self, cuj):
        """Scenario: configure, launch ug codex, and submit its first task without opening a picker.

        Expected: Codex completes the task with GPT Luna selected, without a model override.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        evidence = SessionEvidence(session.home, CODEX)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        with Terminal(session, "catalog-discovery-codex-tui-default", [CODEX]) as tui:
            tui.boot()
            tui.submit(task.prompt)
            tui.task(evidence, task)
            tui.exit_normally()
        assert_completed_task_model(session, CODEX, task.value, CODEX_DEFAULT)
        _assert_inference_evidence(recorder, checkpoint, CODEX, task, CODEX_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.claude
    def test_catalog_discovery_claude_headless_uses_default(self, cuj):
        """Scenario: configure, then run ug claude -p with a file task and no model override.

        Expected: the print task completes with a Sonnet response.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = claude_file_task(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        result = session.run(
            CLAUDE,
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=240,
        )
        task.assert_headless_answer(CLAUDE, result)
        _assert_claude_headless_model(result, CLAUDE_DEFAULT)
        _assert_inference_evidence(recorder, checkpoint, CLAUDE, task, CLAUDE_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.codex
    def test_catalog_discovery_codex_headless_uses_default(self, cuj):
        """Scenario: configure, then run ug codex -- exec with a file task and no model override.

        Expected: the exec task completes with GPT Luna selected.
        """
        session, _, recorder = cuj
        recorder.configure_session(session, ["configure", "--skip-upgrade"])
        task = FileTask(session)
        checkpoint = recorder.checkpoint()
        recorder.prepare_launch()
        result = session.run(
            CODEX, "--", "exec", "--skip-git-repo-check", "--json", task.prompt, timeout=240
        )
        task.assert_headless_answer(CODEX, result)
        assert_completed_task_model(session, CODEX, task.value, CODEX_DEFAULT)
        _assert_inference_evidence(recorder, checkpoint, CODEX, task, CODEX_DEFAULT)
        session.assert_not_routed()

    @pytest.mark.claude
    @pytest.mark.parametrize("model", sorted(CLAUDE_MODELS - {CLAUDE_DEFAULT}))
    def test_catalog_discovery_claude_explicit_model_completes_task(self, cuj, model):
        """Scenario: configure, then run a Claude print task with another compatible scoped model.

        Expected: each Haiku/Kimi task completes with the explicitly requested model.
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
        _assert_claude_headless_model(result, claude.discovery_model_id(model))
        _assert_inference_evidence(recorder, checkpoint, CLAUDE, task, model)
        session.assert_not_routed()

    @pytest.mark.codex
    @pytest.mark.parametrize("model", sorted(CODEX_MODELS - {CODEX_DEFAULT}))
    def test_catalog_discovery_codex_explicit_model_completes_task(self, cuj, model):
        """Scenario: configure, then run a Codex exec task with another compatible scoped model.

        Expected: the Kimi task completes with the explicitly requested model selected.
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
        _assert_inference_evidence(recorder, checkpoint, CODEX, task, model)
        session.assert_not_routed()
