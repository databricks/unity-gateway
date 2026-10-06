"""Resolve and query Databricks SQL resources through public APIs."""

from __future__ import annotations

import json
import time
import urllib.request

DEFAULT_TRACE_TABLE_PREFIX = "unity_gateway"
TRACE_TABLE_SUFFIX = "_otel_spans"


def resolve_trace_table(workspace: str, bearer: str) -> str:
    """Resolve the OTel span table from the workspace's tracing configuration."""
    workspace_request = urllib.request.Request(
        f"{workspace.rstrip('/')}/api/2.0/preview/scim/v2/Me",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(workspace_request, timeout=30) as response:  # noqa: S310
        workspace_id = response.headers.get("x-databricks-org-id")

    assert workspace_id, "Workspace response did not include x-databricks-org-id"
    config_request = urllib.request.Request(
        f"{workspace.rstrip('/')}/api/ai-gateway/v2/tracing-config/workspace/{workspace_id}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(config_request, timeout=30) as response:  # noqa: S310
        config = json.load(response)

    assert config.get("enabled") is True, "Workspace tracing is not enabled"
    catalog = config.get("catalog_name")
    schema = config.get("schema_name")
    prefix = config.get("table_name_prefix") or DEFAULT_TRACE_TABLE_PREFIX
    assert isinstance(catalog, str) and catalog, "Tracing config has no catalog_name"
    assert isinstance(schema, str) and schema, "Tracing config has no schema_name"
    assert isinstance(prefix, str), "Tracing config has an invalid table_name_prefix"
    return ".".join(
        _quote_identifier(identifier)
        for identifier in (catalog, schema, prefix + TRACE_TABLE_SUFFIX)
    )


def _quote_identifier(identifier: str) -> str:
    return f"`{identifier.replace('`', '``')}`"


def resolve_warehouse_id(workspace: str, bearer: str) -> str:
    """Choose an existing warehouse, preferring one that is already running."""
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/api/2.0/sql/warehouses",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        result = json.load(response)

    warehouses = [
        warehouse
        for warehouse in result.get("warehouses", [])
        if isinstance(warehouse, dict) and warehouse.get("id")
    ]
    assert warehouses, "The integration workspace has no SQL warehouse"
    running = next(
        (warehouse for warehouse in warehouses if warehouse.get("state") == "RUNNING"), None
    )
    return str((running or warehouses[0])["id"])


def query_count(
    workspace: str,
    bearer: str,
    warehouse_id: str,
    statement: str,
    parameters: list[dict[str, str]],
) -> int:
    request = urllib.request.Request(
        f"{workspace.rstrip('/')}/api/2.0/sql/statements",
        data=json.dumps(
            {
                "warehouse_id": warehouse_id,
                "statement": statement,
                "parameters": parameters,
                "wait_timeout": "50s",
                "on_wait_timeout": "CONTINUE",
            }
        ).encode(),
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        result = json.load(response)

    deadline = time.monotonic() + 180
    while result.get("status", {}).get("state") in {"PENDING", "RUNNING"}:
        assert time.monotonic() < deadline, "SQL statement did not finish within 180 seconds"
        time.sleep(2)
        poll = urllib.request.Request(
            f"{workspace.rstrip('/')}/api/2.0/sql/statements/{result['statement_id']}",
            headers={"Authorization": f"Bearer {bearer}"},
        )
        with urllib.request.urlopen(poll, timeout=30) as response:  # noqa: S310
            result = json.load(response)

    assert result.get("status", {}).get("state") == "SUCCEEDED", result.get("status")
    rows = result.get("result", {}).get("data_array", [])
    assert rows and rows[0], "SQL count query returned no row"
    return int(rows[0][0])
