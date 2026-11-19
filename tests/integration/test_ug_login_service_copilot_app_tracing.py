"""CUJ: ug's login service forwards the GitHub Copilot app's own spans to the workspace."""

import json
import os
import socket
import subprocess
import time

import pytest
from utils.evidence import fresh_id
from utils.harness import process_group_options, stop_process
from utils.managed import use_managed_config_fixture
from utils.sql import resolve_trace_table, resolve_warehouse_id, wait_for_count

pytestmark = [pytest.mark.live, pytest.mark.copilot]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _copilot_span_line(marker: str, *, name: str, span_id: str, parent: str | None) -> str:
    """One span line in the shape Copilot's file exporter writes (runtime 1.0.106)."""
    now = time.time_ns()
    record = {
        "type": "span",
        "traceId": fresh_id().ljust(32, "0")[:32],
        "spanId": span_id,
        "name": name,
        "kind": 2 if parent else 0,
        "startTime": [now // 1_000_000_000 - 1, now % 1_000_000_000],
        "endTime": [now // 1_000_000_000, now % 1_000_000_000],
        "attributes": {
            "gen_ai.operation.name": "chat" if parent else "invoke_agent",
            "gen_ai.provider.name": "github",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.request.stream": True,
        },
        "status": {"code": 0},
        "events": [],
        "resource": {
            "attributes": {
                "service.name": "github-copilot",
                "service.version": "1.0.106-8",
                "ug_integration_marker": marker,
            },
            "schemaUrl": "https://opentelemetry.io/schemas/1.45.0",
        },
        "instrumentationScope": {"name": "github.copilot", "version": "1.0.106-8"},
    }
    if parent:
        record["parentSpanId"] = parent
    return json.dumps(record, separators=(",", ":")) + "\n"


def test_ug_login_service_forwards_copilot_app_spans(live_session, workspace):
    """Scenario: configure Copilot on a workspace that traces it, run ug's login service the way
    its desktop unit does, and append two spans and a metric to the app's span file in the
    format Copilot's file exporter writes. The desktop app isn't in CI, so the file stands in
    for its runtime's output.

    Expected: the service empties the file, and both spans land in the workspace's trace table
    with their marker.
    """
    session = live_session
    bearer = session.env["DATABRICKS_BEARER"]
    table = resolve_trace_table(workspace, bearer)
    warehouse_id = os.environ.get("UG_INTEGRATION_WAREHOUSE_ID", "").strip()
    warehouse_id = warehouse_id or resolve_warehouse_id(workspace, bearer)
    use_managed_config_fixture(session, "copilot_tracing")
    session.run(
        "configure",
        "--agents",
        "copilot",
        "--workspace",
        workspace,
        "--skip-validate",
        "--skip-upgrade",
        "--disable-databricks-ai-tools",
    )
    session.env["UG_EXPERIMENTAL_COPILOT_APP"] = "1"
    marker = f"ug-copilot-app-trace-{fresh_id()}"
    span_file = session.home / ".ucode" / "copilot-app" / "spans.jsonl"
    span_file.parent.mkdir(parents=True, exist_ok=True)
    agent_id, chat_id = fresh_id().ljust(16, "0")[:16], fresh_id().ljust(16, "1")[:16]
    span_file.write_text(
        _copilot_span_line(marker, name="invoke_agent", span_id=agent_id, parent=None)
        + _copilot_span_line(marker, name="chat", span_id=chat_id, parent=agent_id)
        + '{"type":"metric","name":"gen_ai.client.operation.duration","dataPoints":[]}\n',
        encoding="utf-8",
    )
    port = _free_port()
    # A file, not a pipe: a full pipe (4 KB on Windows) would block the service.
    log_path = session.artifacts / "login-service.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(session.binary), "otel-proxy", "--host", workspace, "--port", str(port)],
        cwd=session.cwd,
        env=session.env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        **process_group_options(),
    )
    try:
        deadline = time.monotonic() + 60
        while span_file.stat().st_size and time.monotonic() < deadline:
            assert proc.poll() is None, "ug otel-proxy exited; see login-service.log"
            time.sleep(1)
        remaining = span_file.read_text(encoding="utf-8")
    finally:
        stop_process(proc)
        proc.wait(timeout=10)
        log.close()
        session.record(
            "login-service.json", {"output": log_path.read_text(encoding="utf-8", errors="replace")}
        )
    assert remaining == "", "the login service didn't forward the app's spans"

    marker_query = (
        f"SELECT COUNT(*) FROM {table} "
        "WHERE time > current_timestamp() - INTERVAL 10 MINUTES "
        "AND variant_get(resource.attributes, '$[\"ug_integration_marker\"]', 'STRING') = :marker "
        "AND variant_get(attributes, '$[\"gen_ai.operation.name\"]', 'STRING') = :operation"
    )
    counts = {
        operation: wait_for_count(
            workspace,
            bearer,
            warehouse_id,
            marker_query,
            [
                {"name": "marker", "value": marker, "type": "STRING"},
                {"name": "operation", "value": operation, "type": "STRING"},
            ],
        )
        for operation in ("invoke_agent", "chat")
    }
    session.record("trace-query.json", {"marker": marker, "table": table, "counts": counts})
    assert counts["invoke_agent"] > 0, f"no invoke_agent span for {marker}"
    assert counts["chat"] > 0, f"no chat span for {marker}"
