"""CUJ: a relayed (subscription-relay) Claude session reaching both its subscription models and,
via the hybrid re-route, Databricks-hosted models."""

import json
import urllib.request

import pytest
from utils.evidence import FileTask, assert_subagent_routed
from utils.managed import use_managed_config_fixture
from utils.model_discovery import claude_model_in_picker
from utils.request_recorder import record_claude_response_protocol, served_inference_request
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.claude, pytest.mark.claude_relay]

# Anthropic gateway model catalog; a Databricks-hosted id here is namespace-qualified, which is
# what the relayed proxy re-routes to gateway auth (bare Anthropic ids stay on the relay path).
ANTHROPIC_MODELS_PATH = "/ai-gateway/anthropic/v1/models"


def _databricks_hosted_model(workspace: str, token: str) -> str:
    """A gateway-served, namespace-qualified model id (prefer the cheapest Claude tier).

    Discovered from the live gateway catalog rather than hardcoded, mirroring how the explicit-model
    CUJs record a real discovered model. Excludes `anthropic-aigw-*` aliases: they need their
    provider-service header to route, which the Databricks re-route drops, so a direct call 404s."""
    request = urllib.request.Request(
        workspace + ANTHROPIC_MODELS_PATH, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        ids = [entry.get("id") for entry in json.load(response).get("data", [])]
    hosted = [
        model for model in ids if model and "." in model and not model.startswith("anthropic-aigw-")
    ]
    haiku = [model for model in hosted if "haiku" in model]
    model = (haiku or hosted or [""])[0]
    assert model, f"gateway served no Databricks-hosted model id to route; catalog was {ids}"
    return model


def test_ug_claude_relayed_serves_subscription_and_databricks_models(
    live_session, workspace, claude_relayed_provider, claude_oauth_token
):
    """Scenario: from fresh state, launch Claude relayed through a subscription-relay MPS and submit
    a headless prompt on two models — a bare Anthropic id the subscription serves, then a
    Databricks-hosted `system.ai` id.

    Expected: the relayed launch runs headless (the OAuth token stands in for the browser login).
    The bare model completes the file task over the relay path (route=relay); the Databricks-hosted
    model completes it too because the loopback proxy re-routes that request to gateway auth
    (route=databricks). One relayed session reaches both.
    """
    session = live_session
    databricks_model = _databricks_hosted_model(workspace, session.env["DATABRICKS_BEARER"])
    session.record(
        "model.json", {"model": databricks_model, "source": "gateway /v1/models discovery"}
    )

    # Headless relayed launches: the OAuth token stands in for the browser subscription login, and
    # the diagnostics flag makes the proxy log its per-request route decision to stderr.
    session.env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth_token
    session.env["UCODE_RELAYED_PROXY_DIAGNOSTICS"] = "1"

    def relayed_file_task(model, *launch_options):
        task = FileTask(session)
        result = session.run(
            "claude",
            *launch_options,
            "--provider",
            claude_relayed_provider,
            "--model",
            model,
            "--",
            "-p",
            task.prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            timeout=240,
        )
        task.assert_headless_answer("claude", result)
        return result

    # Relay path: a bare Anthropic id, served directly by the subscription.
    # The family alias "haiku" can resolve to a configured gateway model. Use
    # the exact subscription model ID to exercise the bare-model relay route.
    #
    # This first launch also sets up the workspace, through the relayed provider. `ug configure`
    # would instead write gateway auth to the machine-wide file on an elevated Windows host, which
    # a relayed launch refuses to start over.
    relay = relayed_file_task("claude-haiku-4-5-20251001", "--workspace", workspace)
    assert '"route":"relay"' in relay.stderr, (
        f"Relayed launch did not use the subscription relay path for a bare model:\n{relay.stderr}"
    )

    # Hybrid re-route: a Databricks-hosted id, re-routed to gateway auth in the same relayed mode.
    databricks = relayed_file_task(databricks_model)
    assert '"route":"databricks"' in databricks.stderr, (
        "Relayed proxy did not re-route the Databricks-hosted model to gateway auth; "
        f"no route=databricks diagnostic in:\n{databricks.stderr}"
    )


@pytest.mark.managed_fixture
@pytest.mark.tui
def test_ug_claude_relayed_picker_and_smart_routing(
    live_session, workspace, claude_relayed_provider, claude_oauth_token
):
    """Scenario: configure a subscription-relay MPS and GLM in a managed static catalog,
    open /model in the real Claude TUI, then delegate a hidden-file task with smart routing.

    Expected: both catalog entries appear in numbered picker rows. The parent uses the
    subscription relay, every router response selects an offered model from this mixed
    catalog, and the routed child and parent return the hidden value. Inference completes
    with real HTTP 200 responses and the native TUI exits normally.
    """
    session = live_session
    fixture = use_managed_config_fixture(session, "claude_relayed_smart_routing")
    config = json.loads(fixture.read_text())["enabled_agents"][0]["config"]
    relay_model = f"{claude_relayed_provider}.claude-haiku-4-5-20251001"
    expected_models = {relay_model, "system.ai.glm-5-3-flash"}
    assert set(config["models"]["sources"]) == expected_models, config
    session.env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_oauth_token
    task = FileTask(session)

    with record_claude_response_protocol(session, workspace) as recorder:
        session.run(
            "configure",
            "--workspace",
            recorder.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
            timeout=240,
        )
        assert session.workspace_state().get("claude_relayed") is True
        with AgentTerminal(
            session, "claude", [str(session.binary), "claude"], "relay-picker-smart-routing"
        ) as tui:
            tui.boot()
            picker = tui.open_model_picker(
                model_visible=lambda screen: all(
                    claude_model_in_picker(screen, model, None) for model in expected_models
                )
            )
            session.record("mixed-model-picker.json", {"screen": picker})
            checkpoint = recorder.checkpoint()
            tui.submit(
                task.delegate_prompt + ' Use the Agent tool with subagent_type "general-purpose"; '
                "create a fresh child, do not fork or resume."
            )
            tui.wait_for_task(
                task,
                completion=lambda: (
                    task.completed(session, "claude")
                    and task.completed(session, "claude", child=True)
                ),
                description="the routed child and parent hidden-file answers",
            )
            tui.wait_for_background_tasks()
            tui.exit_normally()

        task.assert_completed(session, "claude")
        task.assert_completed(session, "claude", child=True)
        assert_subagent_routed(session, "claude", task)
        requests = recorder.requests_after(checkpoint)
        routes = [request for request in requests if request.path.endswith("/routes:select")]
        assert routes, "No real routing request for the relayed task"
        for route in routes:
            assert task.filename in route.payload["task"]["prompt"], route.payload
            options = route.payload["route_options"]
            assert {option["model"] for option in options} == expected_models, options
            assert all(option["harness"] == "claude" for option in options), options
            response = recorder.response_for(route)
            assert response.status_code == 200, response.status_code
            selections = response.payload["route_selection"]
            assert len(selections) == 1, selections
            assert selections[0]["route_option"]["model"] in expected_models, selections

        inferences = [
            request
            for request in requests
            if request.method == "POST" and request.path.endswith("/v1/messages")
        ]
        assert inferences, "No real inference requests for the delegated task"
        for inference in inferences:
            served_inference_request(recorder, requests, inference, "claude")
        diagnostics = [
            json.loads(line.removeprefix("[ucode-relay] "))
            for line in tui.proxy_diagnostics.read_text().splitlines()
            if line.strip()
        ]
        relay_requests = {
            row["request_id"]
            for row in diagnostics
            if row.get("event") == "request_start"
            and row.get("route") == "relay-model-service"
            and row.get("path", "").endswith("/v1/messages")
        }
        assert relay_requests, diagnostics
        assert any(
            row.get("event") == "upstream_headers"
            and row.get("request_id") in relay_requests
            and row.get("status") == 200
            for row in diagnostics
        ), diagnostics
