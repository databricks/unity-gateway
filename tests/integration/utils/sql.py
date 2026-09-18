"""Query a Databricks SQL warehouse through the public Statement Execution API."""

from __future__ import annotations

import json
import time
import urllib.request


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
