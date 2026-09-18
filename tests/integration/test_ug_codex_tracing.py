"""Installed-product customer journey for Codex OpenTelemetry export."""

import os
import time
import uuid

import pytest
from utils.constants import CODEX_TEST_MODEL
from utils.evidence import FileTask
from utils.sql import query_count

pytestmark = [pytest.mark.tracing, pytest.mark.codex]


def test_ug_codex_exports_trace_to_configured_table(live_session, workspace):
    """Scenario: configure tracing and run Codex with a unique prompt marker.

    Expected: after the 30-second ingestion window, the configured tracing table
    contains a Codex span carrying that marker, and the real agent task completed.
    """
    session = live_session
    marker = f"ug-codex-trace-{uuid.uuid4().hex}"
    task = FileTask(session)
    session.run(
        "configure",
        "--agents",
        "codex",
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
    warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
    assert table, "Pass --trace-table for the staging tracing table"
    assert warehouse_id, "Pass --warehouse-id for the staging SQL warehouse"
    count = query_count(
        workspace,
        session.env["DATABRICKS_BEARER"],
        warehouse_id,
        (
            f"SELECT COUNT(*) FROM {table} "
            "WHERE time > current_timestamp() - INTERVAL 10 MINUTES "
            "AND variant_get(attributes, '$[\"ug_integration_marker\"]', 'STRING') = :marker"
        ),
        [{"name": "marker", "value": marker, "type": "STRING"}],
    )
    session.record("trace-query.json", {"marker": marker, "table": table, "count": count})
    assert count > 0
