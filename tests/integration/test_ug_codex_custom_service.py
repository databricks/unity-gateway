"""A real opaque model service must use its destination's Codex reasoning capabilities."""

import json

import pytest
from utils.evidence import FileTask, assert_completed_task_model
from utils.managed import use_managed_config_fixture
from utils.request_recorder import TuiRequestRecorder, assert_served
from utils.terminal import AgentTerminal

pytestmark = [
    pytest.mark.live,
    pytest.mark.tui,
    pytest.mark.codex,
    pytest.mark.managed_fixture,
    pytest.mark.custom_model_service,
]

MODEL_SERVICE = "main.ucode.ci_e2e_opaque"
DESTINATION_MODEL = "models/system.ai.databricks-gpt-5"
RESPONSES_PATH = "/ai-gateway/codex/v1/responses"


def test_ug_codex_custom_service_inherits_reasoning_and_completes_file_task(
    live_session, workspace
):
    """Scenario: configure an opaque custom-service default and complete a native Codex TUI task.

    Expected: live metadata identifies GPT-5, which rejects reasoning effort `none`.
    Without a model or effort override, Codex reads a hidden file value using the service's
    full ID and GPT-5-compatible reasoning metadata. Every task request receives HTTP 200.
    Only the admin configuration input is a fixture; service lookup and inference are live.
    """
    session = live_session
    use_managed_config_fixture(session, "codex_custom_service")
    task = FileTask(session)
    with TuiRequestRecorder(workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        checkpoint = recorder.checkpoint()
        try:
            with AgentTerminal(
                session, "codex", [str(session.binary), "codex"], "opaque-service"
            ) as tui:
                tui.boot()
                tui.submit(task.prompt)
                tui.wait_for_task(task, timeout=300)
                tui.exit_normally()
        finally:
            session.record(
                "custom-service-requests.json",
                [
                    {
                        "model": request.payload.get("model"),
                        "reasoning": request.payload.get("reasoning"),
                    }
                    for request in recorder.requests_after(checkpoint)
                    if request.method == "POST" and request.path == RESPONSES_PATH
                ],
            )
        task.assert_completed(session, "codex")
        assert_completed_task_model(session, "codex", task.value, MODEL_SERVICE)
        session.assert_not_routed()

        lookups = [
            request
            for request in recorder.requests_after(0)
            if request.method == "GET"
            and request.path == f"/api/2.1/unity-catalog/model-services/{MODEL_SERVICE}"
        ]
        assert lookups, "UG did not read the custom service's live destinations"
        for lookup in lookups:
            response = recorder.response_for(lookup)
            assert response.status_code == 200, response.status_code
            destinations = response.payload["config"]["routing"]["destinations"]
            assert len(destinations) == 1, destinations
            assert destinations[0]["pay_per_token_config"]["model"] == DESTINATION_MODEL

        catalog = json.loads((session.home / ".ucode/codex-model-catalog.json").read_text())
        visible = [model for model in catalog["models"] if model["visibility"] == "list"]
        assert [model["slug"] for model in visible] == [MODEL_SERVICE], visible
        assert visible[0]["default_reasoning_level"] == "medium", visible
        assert [level["effort"] for level in visible[0]["supported_reasoning_levels"]] == [
            "low",
            "medium",
            "high",
        ], visible
        requests = [
            request
            for request in recorder.requests_after(checkpoint)
            if request.method == "POST"
            and request.path == RESPONSES_PATH
            and task.filename in json.dumps(request.payload)
        ]
        assert requests, "No Codex request contained the file task"
        observed = []
        for request in requests:
            assert_served(recorder, request, MODEL_SERVICE)
            effort = request.payload["reasoning"]["effort"]
            assert effort == "medium", effort
            observed.append({"model": MODEL_SERVICE, "effort": effort, "status": 200})
        session.record("custom-service-inference.json", observed)
