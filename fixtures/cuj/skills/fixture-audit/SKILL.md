---
name: fixture-audit
description: Audit a CUJ fixture run by calling both managed fixture MCP services.
---

# Fixture audit

Use this skill when asked to audit a CUJ fixture run ID.

1. Call `read_fixture` on `fixture_reader` with the exact run ID.
2. Call `describe_fixture` on `fixture_metadata` with the same run ID.
3. Wait for successful results from both calls. Do not infer or calculate either value.
4. Return `fixture-audit: <reader-value> <metadata-value>` using the values returned by the services.

Both service calls are required even when the summary skill ran earlier in the session.
