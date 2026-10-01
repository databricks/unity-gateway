# CUJ MCP fixture app

This directory is a minimal Databricks App MCP service for Unity Gateway CUJ2
and CUJ3. It exposes stateless Streamable HTTP at `/mcp` and binds to
`DATABRICKS_APP_PORT` (with port `8000` as the local fallback).

The service exposes three tools:

- `read_fixture(run_id)` — returns a deterministic opaque SHA-256 value.
- `describe_fixture(run_id)` — returns a different deterministic opaque value.
- `decoy_status(run_id)` — a decoy tool for checking that tool selectors are
  scoped correctly.

Run identifiers must be 1–128 ASCII letters, digits, `_`, or `-`, starting with
a letter or digit. Values are derived in memory from a domain-separated hash;
the app does not persist data or use secrets. `app.yaml` is the Databricks Apps
runtime command; install dependencies from `requirements.txt`.

For local smoke testing after installing dependencies:

```bash
DATABRICKS_APP_PORT=8000 python app.py
```
