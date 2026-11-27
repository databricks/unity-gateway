"""Installed-product customer journey for GitHub Copilot CLI OpenTelemetry export."""

import os
import time

import pytest
from utils.constants import COPILOT_GPT_TEST_MODEL, COPILOT_HEADLESS_FLAGS
from utils.evidence import FileTask, fresh_id
from utils.managed import use_managed_config_fixture
from utils.sql import resolve_trace_table, resolve_warehouse_id, wait_for_count

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def test_ug_copilot_exports_trace_to_configured_table(live_session, workspace):
    """Scenario: resolve the trace table, configure Copilot, and run a uniquely marked task.

    Expected: the real agent task completes and, after the ingestion window, the
    configured trace table contains a Copilot span carrying the same marker, for the model
    that ran.
    """
    session = live_session
    bearer = session.env["DATABRICKS_BEARER"]
    table = resolve_trace_table(workspace, bearer)
    warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
    warehouse_id = warehouse_id or resolve_warehouse_id(workspace, bearer)
    marker = f"ug-copilot-trace-{fresh_id()}"
    task = FileTask(session)
    use_managed_config_fixture(session, "copilot_tracing")
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.env["OTEL_RESOURCE_ATTRIBUTES"] = f"ug_integration_marker={marker}"
    result = session.run(
        "copilot",
        "--model",
        COPILOT_GPT_TEST_MODEL,
        "-p",
        f"{task.prompt} Trace correlation marker: {marker}",
        *COPILOT_HEADLESS_FLAGS,
        timeout=180,
    )
    task.assert_headless_answer("copilot", result)

    time.sleep(30)
    marker_query = (
        f"SELECT COUNT(*) FROM {table} "
        "WHERE time > current_timestamp() - INTERVAL 10 MINUTES "
        "AND variant_get(resource.attributes, '$[\"ug_integration_marker\"]', 'STRING') = :marker"
    )
    count = wait_for_count(
        workspace,
        bearer,
        warehouse_id,
        marker_query,
        [{"name": "marker", "value": marker, "type": "STRING"}],
    )
    # Copilot may report the registry id or the bare model name, so match on the model's own name.
    model_name = COPILOT_GPT_TEST_MODEL.removeprefix("system.ai.")
    model_count = wait_for_count(
        workspace,
        bearer,
        warehouse_id,
        marker_query
        + " AND variant_get(attributes, '$[\"gen_ai.request.model\"]', 'STRING') LIKE :model",
        [
            {"name": "marker", "value": marker, "type": "STRING"},
            {"name": "model", "value": f"%{model_name}%", "type": "STRING"},
        ],
    )
    session.record(
        "trace-query.json",
        {"marker": marker, "table": table, "count": count, "model_count": model_count},
    )
    assert count > 0
    assert model_count > 0, f"Copilot span for {marker} lacked model {model_name}"
