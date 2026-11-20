"""Live telemetry delivery on the staging workspace with metrics ingestion enabled."""

import pytest
from utils.evidence import FileTask, assert_completed_task_model, assert_no_session_registry_left
from utils.metrics import assert_metrics_delivered
from utils.request_recorder import TuiRequestRecorder, assert_served
from utils.terminal import AgentTerminal

pytestmark = [pytest.mark.live, pytest.mark.anthropic_mps]


@pytest.mark.claude
def test_ug_configure_delivers_metrics_after_exit(live_session, workspace, recorder_tls):
    """Scenario: enable metrics and configure Claude against the live staging workspace.

    Expected: configure succeeds; its detached exporter delivers configure, auth and API
    histograms to the gateway with HTTP 200 after the command returns. This tests command
    telemetry only, not inference.
    """
    session = live_session
    session.env["UG_ENABLE_METRICS_EXPORT"] = "1"
    with TuiRequestRecorder(workspace, tls=recorder_tls) as recorder:
        session.run(
            "configure",
            "--agents",
            "claude",
            "--workspace",
            recorder.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        uploads = assert_metrics_delivered(
            recorder,
            {
                "cli_import_duration_ms": {"command": "configure"},
                "agent_configuration_duration_ms": {"command": "configure", "agent": "claude"},
                "databricks_token_fetch_duration_ms": {
                    "command": "configure",
                    "token_source": "environment",
                },
                "coding_agent_config_fetch_duration_ms": {"command": "configure"},
                "http_request_duration_ms": {
                    "command": "configure",
                    "http_method": "GET",
                    "http_status_code": 200,
                },
            },
        )
        session.record("configure-metrics.json", uploads)


@pytest.mark.copilot
@pytest.mark.tui
def test_ug_copilot_tui_delivers_startup_and_proxy_metrics(
    live_session,
    workspace,
    claude_provider,
    claude_provider_model,
    recorder_tls,
):
    """Scenario: configure Copilot, launch its real TUI through the staging Anthropic MPS,
    and complete a file-reading task with metrics enabled.

    Expected: the answer contains the file value, inference returns HTTP 200, and the live
    session delivers startup and gateway-auth proxy histograms accepted by the metrics API.
    Copilot exits normally. The bounded wait allows the normal 60-second export interval.
    """
    session = live_session
    session.env["UG_ENABLE_METRICS_EXPORT"] = "1"
    task = FileTask(session)
    session.record(
        "copilot-provider.json", {"provider": claude_provider, "model": claude_provider_model}
    )
    with TuiRequestRecorder(workspace, tls=recorder_tls) as recorder:
        session.run(
            "configure",
            "--agents",
            "copilot",
            "--workspace",
            recorder.url,
            "--skip-upgrade",
            "--disable-databricks-ai-tools",
        )
        checkpoint = recorder.checkpoint()
        command = [
            str(session.binary),
            "copilot",
            "--provider",
            claude_provider,
            "--allow-all-tools",
            "--no-auto-update",
            "--disable-builtin-mcps",
        ]
        with AgentTerminal(session, "copilot", command, "copilot-metrics") as tui:
            tui.boot()
            tui.submit(task.prompt)
            tui.wait_for_task(task)
            inference = [
                request
                for request in recorder.requests_after(checkpoint)
                if request.method == "POST" and request.path.endswith("/messages")
            ]
            assert inference, "No Copilot inference passed through the recorder"
            for request in inference:
                assert_served(recorder, request, claude_provider_model)
            proxy_labels = {
                "command": "copilot",
                "agent": "copilot",
                "proxy_mode": "gateway_auth",
                "api_operation": "messages",
                "http_method": "POST",
                "http_status_code": 200,
            }
            uploads = assert_metrics_delivered(
                recorder,
                {
                    "ug_startup_duration_ms": {"command": "copilot", "agent": "copilot"},
                    "dependency_setup_duration_ms": {"command": "copilot", "agent": "copilot"},
                    "proxy_request_duration_ms": {**proxy_labels, "client_disconnected": False},
                    "proxy_upstream_headers_duration_ms": proxy_labels,
                    "proxy_first_byte_duration_ms": proxy_labels,
                    "proxy_response_bytes": proxy_labels,
                },
                after=checkpoint,
            )
            session.record("copilot-metrics.json", uploads)
            tui.exit_normally()
    assert_completed_task_model(session, "copilot", task.value, claude_provider_model)
    assert_no_session_registry_left(session)
