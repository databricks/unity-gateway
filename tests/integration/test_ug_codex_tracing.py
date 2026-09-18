"""Installed-product customer journey for Codex OpenTelemetry export."""

import os
import time
import uuid

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.managed import (
    build_codex_agent_config,
    build_coding_agent_config,
    set_managed_config_stub,
)
from utils.sql import query_count, resolve_warehouse_id

pytestmark = [pytest.mark.live, pytest.mark.managed_fixture, pytest.mark.codex]


def test_ug_codex_exports_trace_to_configured_table(live_session, workspace, tmp_path):
    """Scenario: configure Codex with tracing enabled and run a task with a unique marker.

    Expected: the real agent task completes and, after the ingestion window, the
    configured trace table contains a Codex span carrying the same marker.
    """
    session = live_session
    marker = f"ug-codex-trace-{uuid.uuid4().hex}"
    task = FileTask(session)
    config = build_coding_agent_config(
        "CODING_AGENT_CODEX",
        build_codex_agent_config(models=[CODEX_TEST_MODEL], otel_tracing_enabled=True),
    )
    set_managed_config_stub(session, tmp_path, config)
    session.run(
        "configure",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    assert session.workspace_state().get("codex_otel_tracing") is True

    result = session.run(
        "codex",
        "--",
        "--config",
        f'otel.span_attributes.ug_integration_marker="{marker}"',
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--model",
        CODEX_TEST_MODEL,
        f"{task.prompt} Trace correlation marker: {marker}",
        timeout=180,
    )
    task.assert_headless_answer("codex", result)

    time.sleep(30)
    table = os.environ.get("UG_INTEGRATION_TRACE_TABLE", "").strip()
    assert table, "Pass --trace-table for the configured tracing table"
    warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
    warehouse_id = warehouse_id or resolve_warehouse_id(workspace, session.env["DATABRICKS_BEARER"])
    bearer = session.env["DATABRICKS_BEARER"]
    marker_query = (
        f"SELECT COUNT(*) FROM {table} "
        "WHERE time > current_timestamp() - INTERVAL 10 MINUTES "
        "AND variant_get(attributes, '$[\"ug_integration_marker\"]', 'STRING') = :marker"
    )
    count = query_count(
        workspace,
        bearer,
        warehouse_id,
        marker_query,
        [{"name": "marker", "value": marker, "type": "STRING"}],
    )
    # The span must also carry Codex's `model` attribute matching the model that ran,
    # so the trace is attributable to a specific model and not just to this test run.
    model_count = query_count(
        workspace,
        bearer,
        warehouse_id,
        marker_query + " AND variant_get(attributes, '$[\"model\"]', 'STRING') = :model",
        [
            {"name": "marker", "value": marker, "type": "STRING"},
            {"name": "model", "value": CODEX_TEST_MODEL, "type": "STRING"},
        ],
    )
    session.record(
        "trace-query.json",
        {"marker": marker, "table": table, "count": count, "model_count": model_count},
    )
    assert count > 0
    assert model_count > 0, f"Codex span for {marker} lacked model={CODEX_TEST_MODEL}"
