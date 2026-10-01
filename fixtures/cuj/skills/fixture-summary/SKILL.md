---
name: fixture-summary
description: Read and summarize the CUJ fixture value using the managed fixture reader MCP.
---

# Fixture summary

Use this skill when asked for the CUJ fixture summary for a run ID.

1. Call `read_fixture` on the managed `fixture_reader` MCP service with the exact run ID from the request.
2. Wait for a successful tool result. Do not infer or calculate the value yourself.
3. Return `fixture-summary: <value>` using the value from that result.

The value is deliberately opaque. A tool listing or a copied example is not a result.
